#!/usr/bin/env python3
"""Werewolf agent — make.fun bounty werewolf-agent-01.

A scripted (no model calls) Werewolf agent. It plays one seat in 30 games
of 8-player Werewolf against house agents, through the env at
MAKEFUN_ENV_URL. It reads the brief at MAKEFUN_PROMPT_FILE, answers every
decision the env waits on, and exits when the trial is done.

Strategy, in short:
- Werewolf: coordinate the night kill with the partner (always agree when
  voting second; when first, name the most dangerous villager). By day,
  sound like a villager and push the village toward a mislynch: vary the
  statements, give a specific reason from the log when one exists, ask a
  question, defend a trusted villager, or stay quiet when nothing is new,
  so the house cannot spot one repeated template. On day 1 it may claim
  to be the seer and clear a trusted villager, forcing the real seer into
  a counter-claim; from day 2 it names one villager a day as a wolf it
  "inspected", and backs its partner's seer claim.
- Seer: inspect the most suspicious player each night; announce a found
  wolf in the debate so the village can exile it.
- Doctor: protect the seer claimant when there is one, else the most
  trusted villager; never self, never the same seat twice in a row.
- Villager: vote the most suspicious player from the evidence (votes,
  statements, deaths, seer claims), and say why.

Deterministic: every random choice comes from MAKEFUN_TRIAL_SEED combined
with the game number. No wall clock, no unseeded randomness, and the env is
contacted one request at a time, in order. Python 3 standard library only.
"""

import hashlib
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request

ENV_URL = "MAKEFUN_ENV_URL"
PROMPT_FILE = "MAKEFUN_PROMPT_FILE"
TRIAL_SEED = "MAKEFUN_TRIAL_SEED"

WOLF = "werewolf"
SEER = "seer"
DOCTOR = "doctor"
VILLAGER = "villager"
ROLES = (WOLF, SEER, DOCTOR, VILLAGER)

# ---------------------------------------------------------------- env client


def env_post(base, path, body, timeout=120):
    """POST one JSON object to the env; return the decoded JSON answer."""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": "http_%d" % e.code}
    except Exception as e:  # network hiccup: caller retries by observing again
        return {"error": "net", "message": str(e)}


def act(base, game, decision, target=None, text=None, do_pass=False):
    """Send one action to the env; return True when it was accepted."""
    body = {"game": game, "decision": decision}
    if decision == "speak":
        if do_pass:
            body["pass"] = True
        else:
            body["text"] = text
    else:
        body["target"] = target
    ans = env_post(base, "/act", body)
    return isinstance(ans, dict) and "error" not in ans


# ---------------------------------------------------------------- seeded rng


def rng_for(seed, game):
    """One deterministic RNG per game, from the trial seed and game number."""
    digest = hashlib.sha256(
        ("agent-fun/agent/rng/v1|werewolf|%s|game:%d" % (seed, game)).encode("utf-8")
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# ---------------------------------------------------------------- view parsing

NAME_OF = {}  # filled per view: seat -> name


def fold(text):
    """Fold a statement the env's way: runs of breaks/tabs become one space."""
    return re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text).replace("\x00", "").strip()


def living_players(view):
    return [p for p in view.get("players", []) if p.get("alive")]


def living_seats(view):
    return [p["seat"] for p in living_players(view)]


def revealed_role(view, seat):
    """A player's revealed role (death/exile), or None while hidden."""
    for p in view.get("players", []):
        if p["seat"] == seat:
            return p.get("role")
    return None


def name_of(view, seat):
    for p in view.get("players", []):
        if p["seat"] == seat:
            return p.get("name", "Seat %d" % seat)
    return "Seat %d" % seat


def seat_name(view, seat):
    return "%s (seat %d)" % (name_of(view, seat), seat)


class Evidence:
    """Everything the public log and my private knowledge say, per game."""

    def __init__(self, view):
        self.view = view
        self.me = view["you"]["seat"]
        self.role = view["you"]["role"]
        self.partner = None
        if view["you"].get("partner"):
            self.partner = view["you"]["partner"]["seat"]
        # Certain knowledge.
        self.known_wolf = set()
        self.known_innocent = set()
        # Seer results (my own, when I am the seer).
        for r in view.get("seerResults") or []:
            if r.get("werewolf"):
                self.known_wolf.add(r["seat"])
            else:
                self.known_innocent.add(r["seat"])
        # Revealed roles.
        for p in view.get("players", []):
            if p.get("role") == WOLF:
                self.known_wolf.add(p["seat"])
            elif p.get("role") is not None:
                self.known_innocent.add(p["seat"])
        # I know my partner is a wolf, and I am one, when I am a wolf.
        if self.role == WOLF:
            self.known_wolf.add(self.me)
            if self.partner is not None:
                self.known_wolf.add(self.partner)
        # Claims and accusations parsed from speeches.
        self.seer_claims = {}  # seat -> list of (day, inspected seat or None, said wolf?)
        self.accusations = []  # (day, accuser, accused)
        self.defenses = []  # (day, defender, defended)
        self.votes = []  # (day, voter, target or None)
        self.night_kills = {}  # day -> died seat or None
        self.exiles = {}  # day -> (seat or None, role or None)
        self._parse_log()
        # Suspicion per living seat (higher = more likely a wolf).
        self.suspicion = self._score()

    def _parse_log(self):
        log = self.view.get("log", [])
        for e in log:
            kind = e.get("kind")
            day = e.get("day", 1)
            if kind == "dawn":
                self.night_kills[day] = e.get("died")
            elif kind == "exile":
                self.exiles[day] = (e.get("seat"), e.get("role"))
            elif kind == "vote":
                self.votes.append((day, e.get("seat"), e.get("target")))
            elif kind == "speech":
                self._parse_speech(day, e.get("seat"), e.get("text", ""))

    def _parse_speech(self, day, seat, text):
        low = text.lower()
        # Map names to seats for claims that name a player, not a seat.
        names = {}
        for p in self.view.get("players", []):
            names[p["name"].lower()] = p["seat"]
        # Seer claims: "i am the seer", "i'm the seer", "i inspected X".
        if re.search(r"\bi\s*am the seer\b|\bi'?m the seer\b|\bi am seer\b", low):
            self.seer_claims.setdefault(seat, []).append((day, None, None))
        m = re.search(r"inspected\s+(?:seat\s+)?(\d+)", low)
        if m:
            target = int(m.group(1))
            said_wolf = bool(re.search(r"werewolf|wolf", low))
            self.seer_claims.setdefault(seat, []).append((day, target, said_wolf))
        else:
            m = re.search(r"inspected\s+([a-z][a-z ]*?)(?:\s+and|\s+is|\s*,|\s*$)", low)
            if m and m.group(1).strip() in names:
                target = names[m.group(1).strip()]
                said_wolf = bool(re.search(r"werewolf|wolf", low))
                self.seer_claims.setdefault(seat, []).append((day, target, said_wolf))
        # Accusations: "seat N is a wolf", "N is the werewolf", "vote N",
        # and the same with a player's name.
        for m in re.finditer(r"(?:seat\s+)?(\d+)\s+is\s+(?:a\s+|the\s+)?(?:were)?wolf", low):
            self.accusations.append((day, seat, int(m.group(1))))
        for m in re.finditer(r"vote\s+(?:seat\s+)?(\d+)", low):
            self.accusations.append((day, seat, int(m.group(1))))
        for name, target in names.items():
            if re.search(r"\b" + re.escape(name) + r"\s+is\s+(?:a\s+|the\s+)?(?:were)?wolf", low):
                self.accusations.append((day, seat, target))
            if re.search(r"vote\s+" + re.escape(name) + r"\b", low):
                self.accusations.append((day, seat, target))
        # Defenses: "N is innocent", "N is not a wolf", "trust N", "N is clear".
        for m in re.finditer(
            r"(?:seat\s+)?(\d+)\s+is\s+(?:not a wolf|innocent|clear|safe|a villager)",
            low,
        ):
            self.defenses.append((day, seat, int(m.group(1))))
        for m in re.finditer(r"trust\s+(?:seat\s+)?(\d+)", low):
            self.defenses.append((day, seat, int(m.group(1))))

    def _score(self):
        """Suspicion per living seat from the evidence so far."""
        s = {seat: 0.0 for seat in living_seats(self.view)}
        if self.me in s:
            del s[self.me]
        # Certain knowledge dominates.
        for seat in self.known_wolf:
            if seat in s:
                s[seat] = 1000.0
        for seat in self.known_innocent:
            if seat in s:
                s[seat] = -1000.0
        # Vote and speech evidence, scored once the target's role is known.
        for day, voter, target in self.votes:
            if target is None or voter == self.me:
                continue
            role = revealed_role(self.view, target)
            if role == WOLF:
                s[voter] = s.get(voter, 0.0) - 15.0
            elif role is not None:
                s[voter] = s.get(voter, 0.0) + 10.0
        for day, accuser, accused in self.accusations:
            if accuser == self.me:
                continue
            role = revealed_role(self.view, accused)
            if role == WOLF:
                s[accuser] = s.get(accuser, 0.0) - 10.0
            elif role is not None:
                s[accuser] = s.get(accuser, 0.0) + 5.0
        for day, defender, defended in self.defenses:
            if defender == self.me:
                continue
            role = revealed_role(self.view, defended)
            if role == WOLF:
                s[defender] = s.get(defender, 0.0) + 15.0
            elif role is not None:
                s[defender] = s.get(defender, 0.0) - 3.0
        # A wolf accuses villagers to mislead; a wolf defends its partner.
        for day, accuser, accused in self.accusations:
            if accuser in self.known_wolf and accused not in self.known_wolf:
                s[accused] = s.get(accused, 0.0) - 5.0
        for day, defender, defended in self.defenses:
            if defender in self.known_wolf and defended not in self.known_wolf:
                s[defended] = s.get(defended, 0.0) + 10.0
        # Wolves vote together: a player who repeatedly voted for the same
        # targets as a revealed wolf is likely that wolf's partner.
        by_day_voter = {}
        for day, voter, target in self.votes:
            if target is not None:
                by_day_voter.setdefault((day, voter), target)
        for (day, voter), target in list(by_day_voter.items()):
            if voter in self.known_wolf or voter == self.me:
                continue
            for (d2, voter2), target2 in by_day_voter.items():
                if d2 == day and voter2 in self.known_wolf and target2 == target:
                    s[voter] = s.get(voter, 0.0) + 12.0
        # A player whose votes keep landing on revealed innocents, while
        # never landing on a revealed wolf, is likely a wolf hiding.
        for seat in list(s.keys()):
            if seat in self.known_wolf or seat in self.known_innocent:
                continue
            voted_wolf = 0
            voted_innocent = 0
            for day, voter, target in self.votes:
                if voter != seat or target is None:
                    continue
                role = revealed_role(self.view, target)
                if role == WOLF:
                    voted_wolf += 1
                elif role is not None:
                    voted_innocent += 1
            if voted_innocent >= 2 and voted_wolf == 0:
                s[seat] = s.get(seat, 0.0) + 10.0
        # Two seer claims cannot both be true: the one whose inspections do
        # not match later reveals is likely the lying wolf.
        claimants = list(self.seer_claims.keys())
        if len(claimants) >= 2:
            for c in claimants:
                good = 0
                bad = 0
                for _day, target, said_wolf in self.seer_claims.get(c, []):
                    if target is None or said_wolf is None:
                        continue
                    role = revealed_role(self.view, target)
                    if role is None:
                        continue
                    if (role == WOLF) == said_wolf:
                        good += 1
                    else:
                        bad += 1
                if bad > 0 and good == 0:
                    s[c] = s.get(c, 0.0) + 25.0
                elif good > 0:
                    s[c] = s.get(c, 0.0) - 20.0
        # Quiet players are slightly suspicious late; talkative accusers less so.
        return s

    def claim_record(self, claimant):
        """(good, bad): the claimant's inspections that matched / contradicted
        later reveals. A claimant with bad > 0 and good == 0 is likely lying."""
        good = 0
        bad = 0
        for _day, target, said_wolf in self.seer_claims.get(claimant, []):
            if target is None or said_wolf is None:
                continue
            role = revealed_role(self.view, target)
            if role is None:
                continue
            if (role == WOLF) == said_wolf:
                good += 1
            else:
                bad += 1
        return good, bad

    def credible_seer_wolf(self):
        """A wolf named by a credible seer claim, or None.

        A claim names a wolf when a player said they inspected a seat and
        it is a werewolf. It is credible when the claimant is not a known
        wolf, the named seat is alive and not known innocent, and the
        claimant's past inspections check out against revealed roles (a
        claimant whose inspections were contradicted and never verified is
        likely the lying wolf). With several claimants, the one with
        verified inspections and the most recent claim wins; ties break to
        the target I already suspect most.
        """
        best = None
        best_score = None
        for claimant, claims in self.seer_claims.items():
            if claimant == self.me or claimant in self.known_wolf:
                continue
            good, bad = self.claim_record(claimant)
            if bad > 0 and good == 0:
                continue  # contradicted and never verified: likely lying
            for day, target, said_wolf in claims:
                if target is None or not said_wolf:
                    continue
                if target not in living_seats(self.view):
                    continue
                if target in self.known_innocent:
                    continue
                score = (
                    self.suspicion.get(target, 0.0)
                    + 20.0 * good
                    - 25.0 * bad
                    + 5.0 * day
                )
                if best is None or score > best_score:
                    best, best_score = target, score
        return best

    def most_suspicious(self, exclude=()):
        """Living seat with the highest suspicion, excluding given seats."""
        best = None
        best_score = None
        for seat, score in self.suspicion.items():
            if seat in exclude or seat == self.me:
                continue
            if best is None or score > best_score:
                best, best_score = seat, score
        return best, best_score

    def least_suspicious(self, exclude=()):
        best = None
        best_score = None
        for seat, score in self.suspicion.items():
            if seat in exclude or seat == self.me:
                continue
            if best is None or score < best_score:
                best, best_score = seat, score
        return best, best_score

    def day_votes(self, day):
        """Votes cast so far in this day's voting: {target: count}."""
        tally = {}
        for d, voter, target in self.votes:
            if d == day and target is not None:
                tally[target] = tally.get(target, 0) + 1
        return tally


# ---------------------------------------------------------------- decisions


def choose_kill(ev, options, rng):
    """Werewolf night kill. Agree with the partner when they voted first."""
    view = ev.view
    day = view["day"]
    # The partner's vote tonight, if they already named someone.
    for v in view.get("wolfVotes") or []:
        if v.get("night") == day and v.get("seat") == ev.partner and v.get("target") is not None:
            if v["target"] in options:
                return v["target"]
    # I vote first (or the partner abstained): name the most dangerous
    # villager. A credible seer claimant first, then the most trusted
    # (least suspicious) villager, since the village trusts them.
    # Never name a seat the village already suspects: they may be exiled
    # by day anyway, and the kill is better spent elsewhere.
    candidates = [o for o in options if o not in ev.known_wolf]
    if not candidates:
        return None
    # A seer claimant is the best kill: they can expose us.
    claimants = [c for c in ev.seer_claims if c in candidates and c != ev.me]
    if claimants:
        # The claimant whose inspections checked out is the real seer.
        best = None
        best_good = -1
        for c in claimants:
            good = 0
            for _d, target, said_wolf in ev.seer_claims.get(c, []):
                if target is None or said_wolf is None:
                    continue
                role = revealed_role(view, target)
                if role is not None and (role == WOLF) == said_wolf:
                    good += 1
            if good > best_good:
                best, best_good = c, good
        if best is not None:
            return best
    # Otherwise the least suspicious living villager: the village's
    # most trusted voice. Break ties with the seeded RNG.
    scored = []
    for seat in candidates:
        scored.append((ev.suspicion.get(seat, 0.0), seat))
    scored.sort(key=lambda t: (t[0], rng.random()))
    return scored[0][1]


def choose_protect(ev, options, rng):
    """Doctor night protection. Guard the seer claimant, else the most
    trusted villager. Never self; never last night's seat (not an option)."""
    view = ev.view
    candidates = [o for o in options if o != ev.me]
    if not candidates:
        return None
    claimants = []
    for c in ev.seer_claims:
        if c not in candidates or c == ev.me:
            continue
        good, bad = ev.claim_record(c)
        if bad > 0 and good == 0:
            continue  # discredited: likely a lying wolf, not worth guarding
        claimants.append(c)
    if claimants:
        best = None
        best_good = -1
        for c in claimants:
            good = 0
            for _d, target, said_wolf in ev.seer_claims.get(c, []):
                if target is None or said_wolf is None:
                    continue
                role = revealed_role(view, target)
                if role is not None and (role == WOLF) == said_wolf:
                    good += 1
            if good > best_good:
                best, best_good = c, good
        if best is not None:
            return best
    scored = []
    for seat in candidates:
        scored.append((ev.suspicion.get(seat, 0.0), seat))
    scored.sort(key=lambda t: (t[0], rng.random()))
    return scored[0][1]


def choose_inspect(ev, options, rng):
    """Seer night inspection: the most suspicious player not yet inspected."""
    done = {r["seat"] for r in (ev.view.get("seerResults") or [])}
    # A rival seer claimant must be checked first: inspecting them settles
    # whether they are the real seer or a lying wolf.
    rivals = [c for c in ev.seer_claims if c != ev.me and c in options and c not in done]
    if rivals:
        scored = []
        for seat in rivals:
            scored.append((ev.suspicion.get(seat, 0.0), seat))
        scored.sort(key=lambda t: (-t[0], rng.random()))
        return scored[0][1]
    candidates = [o for o in options if o not in done and o != ev.me]
    if not candidates:
        candidates = [o for o in options if o != ev.me]
    if not candidates:
        return None
    scored = []
    for seat in candidates:
        scored.append((ev.suspicion.get(seat, 0.0), seat))
    scored.sort(key=lambda t: (-t[0], rng.random()))
    return scored[0][1]


def choose_vote(ev, options, rng):
    """Day vote. Village: the most suspicious player. Wolf: the villager
    the village already suspects most (push the mislynch), never the
    partner. Seer with a found wolf: that wolf."""
    view = ev.view
    day = view["day"]
    if ev.role == SEER:
        for seat in ev.known_wolf:
            if seat in options and seat != ev.me:
                return seat
    # A credible seer claim naming a wolf is the village's best lead:
    # follow it before my own suspicion.
    if ev.role != WOLF:
        claimed = ev.credible_seer_wolf()
        if claimed is not None and claimed in options and claimed != ev.me:
            return claimed
    tally = ev.day_votes(day)
    if ev.role == WOLF:
        # Never vote the partner. Vote the wolf I (or my partner) named in
        # a seer claim first: a seer who does not vote their own find is
        # not believed. Prefer a villager who already has votes (the
        # village is turning on them); else the most suspicious villager
        # by the village's own reasoning.
        pool = [o for o in options if o not in ev.known_wolf]
        if not pool:
            return None
        for claimant in (ev.me, ev.partner):
            if claimant is None:
                continue
            for _d, tgt, said_wolf in ev.seer_claims.get(claimant, []):
                if tgt is not None and said_wolf and tgt in pool:
                    return tgt
        # A quarter of the time, vote my own most-suspicious villager
        # instead of the wagon: always joining the plurality is easy
        # for the house to spot.
        if rng.random() < 0.25:
            target, _score = ev.most_suspicious(exclude=set(ev.known_wolf))
            if target is not None and target in pool:
                return target
        with_votes = [(tally.get(s, 0), ev.suspicion.get(s, 0.0), s) for s in pool]
        with_votes.sort(key=lambda t: (-t[0], -t[1], rng.random()))
        return with_votes[0][2]
    # Village side: the most suspicious living player. A found wolf first.
    for seat in ev.known_wolf:
        if seat in options and seat != ev.me:
            return seat
    scored = []
    for seat in options:
        if seat == ev.me:
            continue
        scored.append((ev.suspicion.get(seat, 0.0), tally.get(seat, 0), seat))
    if not scored:
        return None
    # Weight the current tally: a suspicious player with votes is closer
    # to exile, and joining the plurality is how the village wins.
    scored.sort(key=lambda t: (-(t[0] + 8.0 * t[1]), rng.random()))
    best = scored[0]
    # On day 1 with no evidence at all, a random vote is as good as any;
    # abstaining stalls the game, so still vote.
    return best[2]


# ---------------------------------------------------------------- speech


def _my_last_speech(ev):
    """(index, text) of my most recent statement in the log, or (None, None)."""
    log = ev.view.get("log", [])
    for i in range(len(log) - 1, -1, -1):
        e = log[i]
        if e.get("kind") == "speech" and e.get("seat") == ev.me:
            return i, e.get("text", "")
    return None, None


def _anything_new_since(ev, index):
    """True when a dawn, an exile, or another player's statement came after
    my statement at log index `index`."""
    if index is None:
        return True
    for e in ev.view.get("log", [])[index + 1:]:
        if e.get("kind") in ("dawn", "exile", "speech"):
            return True
    return False


def _my_inspected_claim_today(ev):
    """True when I already announced an inspection result today. A real
    seer inspects once a night, so two announced inspections in one day
    would contradict each other."""
    day = ev.view["day"]
    for e in ev.view.get("log", []):
        if (
            e.get("kind") == "speech"
            and e.get("seat") == ev.me
            and e.get("day") == day
            and "inspected seat" in e.get("text", "").lower()
        ):
            return True
    return False


def _my_claimed_wolf(ev):
    """The seat I most recently named as a werewolf in a seer claim."""
    target = None
    for _d, tgt, said_wolf in ev.seer_claims.get(ev.me, []):
        if tgt is not None and said_wolf:
            target = tgt
    return target


def _incriminating_reason(ev, target):
    """A specific, checkable reason to suspect `target`, from the log."""
    view = ev.view
    for _day, voter, tgt in ev.votes:
        if voter == target and tgt is not None:
            role = revealed_role(view, tgt)
            if role is not None and role != WOLF:
                return "seat %d voted to exile seat %d, who turned out to be a %s" % (
                    target, tgt, role)
    for _day, defender, defended in ev.defenses:
        if defender == target and revealed_role(view, defended) == WOLF:
            return "seat %d defended seat %d, who turned out to be a werewolf" % (
                target, defended)
    for _day, voter, tgt in ev.votes:
        if voter == target and tgt is None:
            return "seat %d abstained when the village needed a vote" % target
    return None


def _trust_reason(ev, seat):
    """A specific, checkable reason to trust `seat`, from the log."""
    view = ev.view
    for _day, voter, tgt in ev.votes:
        if voter == seat and tgt is not None and revealed_role(view, tgt) == WOLF:
            return "seat %d voted to exile seat %d, who turned out to be a werewolf" % (
                seat, tgt)
    for _day, defender, defended in ev.defenses:
        if defender == seat and defended is not None:
            role = revealed_role(view, defended)
            if role is not None and role != WOLF:
                return "seat %d defended seat %d, who turned out to be a %s" % (
                    seat, defended, role)
    return None


def wolf_speech(ev, rng):
    """A werewolf's statement: sound like a villager.

    The house reads every word, so a wolf that only repeats one template
    accusing whoever the village already doubts is easy to spot. This
    version varies: it gives a specific reason from the log when one
    exists, asks a question, defends a trusted villager, or stays quiet
    when nothing is new. On day 1 it may also claim to be the seer and
    clear a trusted villager, which forces the real seer into a
    counter-claim and can get the real seer exiled instead of a wolf.
    """
    view = ev.view
    me = ev.me
    day = view["day"]
    # A credible seer claim naming me or my partner is the real seer
    # exposing us: deny it and turn the doubt back on the claimant.
    named_us = ev.credible_seer_wolf()
    if named_us is not None and named_us in ev.known_wolf:
        claimant = None
        for c, claims in ev.seer_claims.items():
            for _d, target, said_wolf in claims:
                if target == named_us and said_wolf:
                    claimant = c
                    break
            if claimant is not None:
                break
        if claimant is not None:
            templates = [
                "Seat {claimant} is lying about being the seer. Their claim does not add up, and they are trying to get a villager exiled.",
                "Do not trust seat {claimant}'s seer claim. I have voted with the village every day, and seat {claimant} is pointing at the wrong people.",
            ]
            return rng.choice(templates).format(claimant=claimant)
    # If someone accused me directly, defend briefly and turn it back.
    accused_me = [a for _d, a, t in ev.accusations if t == me]
    if accused_me:
        templates = [
            "I am not a wolf. Seat {accuser} is pushing me without evidence; look at who benefits from that.",
            "This accusation against me has no basis. I have voted with the village every day.",
            "I am a villager. Seat {accuser} keeps pointing at me instead of looking for the real wolves.",
        ]
        return rng.choice(templates).format(accuser=accused_me[-1])
    # Back my partner's seer claim once, when I have not claimed myself.
    if (
        ev.partner is not None
        and ev.partner in ev.seer_claims
        and me not in ev.seer_claims
    ):
        _idx, last = _my_last_speech(ev)
        if last is None or "believe seat %d" % ev.partner not in last:
            named = None
            for _d, target, said_wolf in ev.seer_claims.get(ev.partner, []):
                if target is not None and said_wolf:
                    named = target
            if named is not None:
                return "I believe seat %d: seat %d is a werewolf. Vote seat %d." % (
                    ev.partner, named, named)
            return "Seat %d's seer claim sounds right to me. Let us follow it." % ev.partner
    # Day 1 fake seer claim: clear the villager the village trusts most.
    # Only before anyone else has claimed, while I am not under suspicion
    # and my partner is alive. A clear is low risk: it is true (the
    # target really is a villager), and it forces the real seer to argue
    # against a claim that has not been wrong yet.
    if (
        day == 1
        and me not in ev.seer_claims
        and not ev.seer_claims
        and ev.partner is not None
        and ev.partner in living_seats(view)
    ):
        cleared, _score = ev.least_suspicious(exclude=set(ev.known_wolf))
        if cleared is not None:
            return (
                "I am the seer. I inspected seat %d on night 1 and they are "
                "not a werewolf." % cleared
            )
    # I claimed seer earlier: from day 2, name one villager a day as a
    # wolf I "inspected". Never two in one day: a real seer inspects
    # once a night, and the house reads the contradiction.
    if me in ev.seer_claims and day >= 2 and not _my_inspected_claim_today(ev):
        target, _score = ev.most_suspicious(exclude=set(ev.known_wolf))
        if target is not None:
            return (
                "I inspected seat %d last night and they are a werewolf. "
                "Vote seat %d today." % (target, target)
            )
    # Stay quiet when nothing is new since my last statement: a wolf that
    # talks every round in the same template is easy to spot.
    idx, last = _my_last_speech(ev)
    if idx is not None and not _anything_new_since(ev, idx) and rng.random() < 0.5:
        return None
    # Otherwise: a specific accusation, a question, or a defense of a
    # trusted villager, each with a reason from the log when one exists.
    roll = rng.random()
    if roll < 0.20:
        # Defend the villager the village trusts most.
        trusted, _score = ev.least_suspicious(exclude=set(ev.known_wolf))
        if trusted is not None:
            reason = _trust_reason(ev, trusted)
            if reason is not None:
                return "Seat %d is not a wolf. %s." % (
                    trusted, reason[0].upper() + reason[1:])
            return "I do not think seat %d is a wolf. They have been steady all game." % trusted
    if roll < 0.35:
        target, _score = ev.most_suspicious(exclude=set(ev.known_wolf))
        if target is not None:
            templates = [
                "What do others make of seat {target}? I am not sure yet, but something feels off.",
                "Has seat {target} explained their votes? I would like to hear it before we decide.",
                "I am still watching seat {target}. Can anyone vouch for them?",
            ]
            return rng.choice(templates).format(target=target)
    pool = [o for o in living_seats(view) if o not in ev.known_wolf and o != me]
    if not pool:
        return None
    target, _score = ev.most_suspicious(exclude=set(ev.known_wolf))
    if target is None or target == me:
        target = rng.choice(pool)
    reason = _incriminating_reason(ev, target)
    if reason is not None:
        templates = [
            "Seat {target} looks like the werewolf to me. {reason}.",
            "Look at seat {target}: {reason}. I think seat {target} is the one to exile today.",
        ]
        return rng.choice(templates).format(target=target, reason=reason)
    templates = [
        "I find seat {target} suspicious. Their votes have not lined up with what the village needed.",
        "Watch seat {target}: they keep deflecting whenever the talk turns to them.",
        "Seat {target} has been quiet at the wrong moments. I think we should look at them today.",
        "I agree with the doubts about seat {target}. Something about their story does not add up.",
        "Has anyone else noticed seat {target} avoiding the hard questions? That worries me.",
        "I am leaning toward seat {target} today. Their reasoning feels off to me.",
    ]
    return rng.choice(templates).format(target=target)


def villager_speech(ev, rng):
    """A villager/doctor statement: share the best suspicion with a reason."""
    view = ev.view
    # Back a credible seer claim first: it is the village's best lead.
    claimed = ev.credible_seer_wolf()
    if claimed is not None:
        claimant = None
        for c, claims in ev.seer_claims.items():
            for _d, target, said_wolf in claims:
                if target == claimed and said_wolf:
                    claimant = c
                    break
            if claimant is not None:
                break
        if claimant is not None:
            # Rebroadcast the claim with the seat number right after
            # "inspected", so every player at the table can follow it.
            templates = [
                "Seat {claimant} inspected seat {target} and seat {target} is a werewolf. Vote seat {target} today.",
                "I believe seat {claimant}'s seer claim: seat {target} is a werewolf. Vote seat {target}.",
                "The seer claim on seat {target} is the strongest lead we have. I am voting seat {target}.",
            ]
            return rng.choice(templates).format(claimant=claimant, target=claimed)
    target, score = ev.most_suspicious()
    if target is None:
        templates = [
            "No one stands out to me yet. Let us hear more before we vote.",
            "I have no strong read so far. I am listening.",
        ]
        return rng.choice(templates)
    # A reason, when the evidence gives one. Incriminating: the target
    # voted to exile someone later revealed innocent, or defended a
    # revealed wolf.
    reason = None
    for day, voter, tgt in ev.votes:
        if voter == target and tgt is not None:
            role = revealed_role(view, tgt)
            if role is not None and role != WOLF:
                reason = "seat %d voted to exile seat %d, who turned out to be a %s" % (
                    target,
                    tgt,
                    role,
                )
                break
    if reason is None:
        for day, defender, defended in ev.defenses:
            if defender == target and revealed_role(view, defended) == WOLF:
                reason = "seat %d defended seat %d, who turned out to be a werewolf" % (
                    target,
                    defended,
                )
                break
    if reason is None:
        reason = "seat %d has been the least consistent voice at this table" % target
    templates = [
        "Seat {target} looks like the werewolf to me. {reason}.",
        "Seat {target} worries me. {reason}.",
        "Look at seat {target}: {reason}. I think seat {target} is the one to exile today.",
    ]
    return rng.choice(templates).format(target=target, reason=reason)


def seer_speech(ev, rng):
    """A seer's statement: announce a found wolf, or share suspicion."""
    view = ev.view
    found = [s for s in ev.known_wolf if s != ev.me and s in living_seats(view)]
    if found:
        target = found[0]
        night = None
        for r in view.get("seerResults") or []:
            if r["seat"] == target:
                night = r["night"]
        if night is not None:
            return (
                "I am the seer. I inspected seat %d on night %d and they are a werewolf. "
                "Vote seat %d today." % (target, night, target)
            )
        return "I am the seer, and seat %d is a werewolf. Vote seat %d today." % (target, target)
    return villager_speech(ev, rng)


def choose_speech(ev, rng):
    """Pick this turn's statement, or None to pass."""
    if ev.role == WOLF:
        text = wolf_speech(ev, rng)
    elif ev.role == SEER:
        text = seer_speech(ev, rng)
    else:
        text = villager_speech(ev, rng)
    if text is None:
        return None
    text = fold(text)
    if not text:
        return None
    # Keep it short: the env caps statements at 500 characters.
    if len(text) > 480:
        text = text[:480].rsplit(" ", 1)[0]
    return text


# ---------------------------------------------------------------- main loop


def decide(base, game, pending, seed):
    """Answer one pending decision. Returns True when the env accepted it."""
    decision = pending["decision"]
    view = pending["view"]
    rng = rng_for(seed, game)
    ev = Evidence(view)
    options = pending.get("options") or []
    if decision == "speak":
        text = choose_speech(ev, rng)
        if text is None:
            return act(base, game, "speak", do_pass=True)
        if not act(base, game, "speak", text=text):
            return act(base, game, "speak", do_pass=True)
        return True
    if decision == "kill":
        return act(base, game, "kill", target=choose_kill(ev, options, rng))
    if decision == "protect":
        return act(base, game, "protect", target=choose_protect(ev, options, rng))
    if decision == "inspect":
        return act(base, game, "inspect", target=choose_inspect(ev, options, rng))
    if decision == "vote":
        return act(base, game, "vote", target=choose_vote(ev, options, rng))
    return False


def main():
    env_url = os.environ.get(ENV_URL, "").rstrip("/")
    prompt_file = os.environ.get(PROMPT_FILE, "")
    seed = os.environ.get(TRIAL_SEED, "werewolf-agent-01")
    if not env_url:
        sys.stderr.write("MAKEFUN_ENV_URL is not set\n")
        return 1
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                f.read(1)
        except OSError as e:
            sys.stderr.write("cannot read prompt file: %s\n" % e)
            return 1
    answered = 0
    while True:
        obs = env_post(env_url, "/observe", {})
        if not isinstance(obs, dict) or "error" in obs:
            # The env is unreachable or answered oddly: try again. A
            # failed /observe never answers a decision, so retrying is safe.
            continue
        if obs.get("done"):
            break
        pending = obs.get("pending") or []
        if not pending:
            continue
        for p in pending:
            try:
                if decide(env_url, p["game"], p, seed):
                    answered += 1
            except Exception as e:
                # Never stall a game on an unexpected error: abstain/pass.
                try:
                    if p["decision"] == "speak":
                        act(env_url, p["game"], "speak", do_pass=True)
                    else:
                        act(env_url, p["game"], p["decision"], target=None)
                except Exception:
                    pass
                sys.stderr.write("game %s %s: %s\n" % (p.get("game"), p.get("decision"), e))
    sys.stdout.write("werewolf agent: %d decisions answered, trial done\n" % answered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
