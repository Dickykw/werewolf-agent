#!/usr/bin/env python3
"""Mock house LLM for local Werewolf testing.

Implements the env's house lane: POST /v1/chat/completions with the
OpenAI-style body the env sends (model, messages, max_tokens,
reasoning_effort), and answers with a heuristic Werewolf player.

The mock parses the user message text the env builds (the [moderator]
lines) and plays a plausible but beatable game: it tracks revealed roles,
scores voters, coordinates wolf kills, and speaks in templates. Its
randomness is seeded by hashing the whole user message, so the same
prompt always gets the same answer (the env's replay stays consistent).

Run: python3 mock_house.py [port]   (default 18081)
"""
import hashlib
import json
import random
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WOLF = "werewolf"
SEER = "seer"
DOCTOR = "doctor"
VILLAGER = "villager"


def parse_state(text):
    """Parse the env's user message into a small structured state."""
    st = {
        "me": None, "name": None, "role": None, "alive": True,
        "partner": None, "players": {},  # seat -> (name, alive, role or None)
        "wolf_votes": [],  # (night, seat, target or None)
        "inspected": {},  # seat -> werewolf bool
        "protected": [],  # (night, seat or None)
        "votes": [],  # (voter, target or None)
        "accusations": [],  # (accuser, accused)
        "seer_claims": [],  # (claimant, target or None, said_wolf or None)
        "decision": None, "options": [], "max_chars": 500,
    }
    m = re.search(r"You are seat (\d+) \(([^)]+)\), a (werewolf|seer|doctor|villager), (alive|dead)\.", text)
    if m:
        st["me"] = int(m.group(1)); st["name"] = m.group(2); st["role"] = m.group(3)
        st["alive"] = m.group(4) == "alive"
    m = re.search(r"Your partner is seat (\d+) \(([^)]+)\), (alive|dead)\.", text)
    if m:
        st["partner"] = int(m.group(1))
    for m in re.finditer(r"seat (\d+) \(([^)]+)\): (alive|dead, was a (werewolf|seer|doctor|villager))", text):
        seat = int(m.group(1))
        role = m.group(4) if m.group(4) else None
        st["players"][seat] = (m.group(2), m.group(3) == "alive", role)
    for m in re.finditer(r"Night (\d+): seat (\d+) \([^)]+\) named (?:seat (\d+)|nobody)", text):
        st["wolf_votes"].append((int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None))
    for m in re.finditer(r"you inspected seat (\d+): (a werewolf|not a werewolf)", text):
        st["inspected"][int(m.group(1))] = m.group(2) == "a werewolf"
    for m in re.finditer(r"you protected (?:seat (\d+)|nobody)", text):
        st["protected"].append(int(m.group(1)) if m.group(1) else None)
    for m in re.finditer(r"seat (\d+) \([^)]+\) voted to exile seat (\d+)", text):
        st["votes"].append((int(m.group(1)), int(m.group(2))))
    for m in re.finditer(r"seat (\d+) \([^)]+\) abstained", text):
        st["votes"].append((int(m.group(1)), None))
    for m in re.finditer(r"seat (\d+) \([^)]+\) said: \"(.*)\"", text):
        seat = int(m.group(1))
        low = m.group(2).lower()
        if re.search(r"\bi\s*am the seer\b|\bi'?m the seer\b", low):
            st["seer_claims"].append((seat, None, None))
        mm = re.search(r"inspected\s+(?:seat\s+)?(\d+)", low)
        if mm:
            st["seer_claims"].append((seat, int(mm.group(1)), "werewolf" in low or "wolf" in low))
        for a in re.finditer(r"(?:seat\s+)?(\d+)\s+is\s+(?:a\s+|the\s+)?(?:were)?wolf", low):
            st["accusations"].append((seat, int(a.group(1))))
        for a in re.finditer(r"vote\s+(?:seat\s+)?(\d+)", low):
            st["accusations"].append((seat, int(a.group(1))))
    # Decision + options.
    if "It is your turn to speak" in text:
        st["decision"] = "speak"
        m = re.search(r"at most (\d+) characters", text)
        if m:
            st["max_chars"] = int(m.group(1))
    elif "It is your turn to vote" in text:
        st["decision"] = "vote"
    elif "Name tonight's victim" in text:
        st["decision"] = "kill"
    elif "Protect one of" in text:
        st["decision"] = "protect"
    elif "Inspect one of" in text:
        st["decision"] = "inspect"
    if st["decision"] in ("vote", "kill", "protect", "inspect"):
        m = re.search(r"one of: (.+?)\.\n", text, re.S)
        if m:
            st["options"] = [int(x) for x in re.findall(r"seat (\d+)", m.group(1))]
    return st


def suspicion(st):
    """Simple suspicion per living seat: vote/speech evidence only."""
    s = {}
    alive = [seat for seat, (_n, a, _r) in st["players"].items() if a]
    for seat in alive:
        if seat != st["me"]:
            s[seat] = 0.0
    revealed = {seat: role for seat, (_n, _a, role) in st["players"].items() if role}
    for voter, target in st["votes"]:
        if target is None or voter == st["me"]:
            continue
        role = revealed.get(target)
        if role == WOLF:
            s[voter] = s.get(voter, 0.0) - 2.0
        elif role is not None:
            s[voter] = s.get(voter, 0.0) + 1.5
    for accuser, accused in st["accusations"]:
        if accuser == st["me"]:
            continue
        role = revealed.get(accused)
        if role == WOLF:
            s[accuser] = s.get(accuser, 0.0) - 1.5
        elif role is not None:
            s[accuser] = s.get(accuser, 0.0) + 0.5
    for seat, wolf in st["inspected"].items():
        if seat in s:
            s[seat] = 100.0 if wolf else -100.0
    return s


def decide(st, rng):
    """One heuristic decision. Returns the JSON object to send back."""
    role = st["role"]
    me = st["me"]
    opts = st["options"]
    s = suspicion(st)
    alive = [seat for seat, (_n, a, _r) in st["players"].items() if a]
    if st["decision"] == "kill":
        # Match the partner's pick tonight when they voted first.
        night = max([n for n, _s, _t in st["wolf_votes"]] or [1])
        for n, seat, target in st["wolf_votes"]:
            if n == night and seat == st["partner"] and target is not None and target in opts:
                return {"target": target, "thought": "agreeing with my partner"}
        pool = [o for o in opts if o != me]
        if not pool:
            return {"target": None, "thought": "nobody to kill"}
        # Kill the most trusted villager (least suspicious), with noise.
        pool.sort(key=lambda x: (s.get(x, 0.0), rng.random()))
        if rng.random() < 0.25:
            return {"target": rng.choice(pool), "thought": "picking a target"}
        return {"target": pool[0], "thought": "killing the most trusted villager"}
    if st["decision"] == "protect":
        pool = [o for o in opts if o != me]
        if not pool:
            return {"target": None, "thought": "nobody to protect"}
        return {"target": rng.choice(pool), "thought": "protecting someone likely to be attacked"}
    if st["decision"] == "inspect":
        pool = [o for o in opts if o not in st["inspected"] and o != me]
        if not pool:
            pool = [o for o in opts if o != me]
        if not pool:
            return {"target": None, "thought": "nobody to inspect"}
        pool.sort(key=lambda x: (-s.get(x, 0.0), rng.random()))
        return {"target": pool[0], "thought": "inspecting the most suspicious player"}
    if st["decision"] == "vote":
        if role == WOLF:
            pool = [o for o in opts if o != st["partner"]]
        else:
            pool = list(opts)
        if not pool:
            return {"target": None, "thought": "abstaining"}
        # A found wolf (seer) or a seer claim naming a wolf wins the vote.
        for seat, wolf in st["inspected"].items():
            if wolf and seat in pool:
                return {"target": seat, "thought": "voting the inspected werewolf"}
        # A player who claimed seer and named a wolf: follow the claim.
        for claimant, target, said_wolf in st["seer_claims"]:
            if target is not None and said_wolf and target in pool and claimant != me:
                return {"target": target, "thought": "following the seer claim"}
        pool.sort(key=lambda x: (-s.get(x, 0.0), rng.random()))
        if rng.random() < 0.2:
            return {"target": rng.choice(pool), "thought": "voting on a hunch"}
        return {"target": pool[0], "thought": "voting the most suspicious player"}
    # speak
    if rng.random() < 0.3:
        return {"say": None, "thought": "passing this round"}
    if role == WOLF:
        pool = [o for o in alive if o != me and o != st["partner"]]
        if not pool:
            return {"say": None, "thought": "passing"}
        target = max(pool, key=lambda x: s.get(x, 0.0))
        name = st["players"].get(target, ("?", True, None))[0]
        lines = [
            "I find %s suspicious. Their story does not add up." % name,
            "Watch %s: they keep deflecting." % name,
            "I am leaning toward %s today." % name,
        ]
        return {"say": rng.choice(lines), "thought": "blending in"}
    found = [seat for seat, wolf in st["inspected"].items() if wolf]
    if role == SEER and found:
        name = st["players"].get(found[0], ("?", True, None))[0]
        return {"say": "I am the seer. I inspected %s and they are a werewolf. Vote them." % name,
                "thought": "revealing the wolf"}
    pool = [o for o in alive if o != me]
    if not pool:
        return {"say": None, "thought": "passing"}
    target = max(pool, key=lambda x: s.get(x, 0.0))
    name = st["players"].get(target, ("?", True, None))[0]
    return {"say": "I suspect %s. We should look at them today." % name,
            "thought": "sharing suspicion"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        messages = body.get("messages", [])
        user_text = ""
        for m in messages:
            if m.get("role") == "user":
                user_text = m.get("content", "")
        # Deterministic per prompt: seed from the whole user message.
        digest = hashlib.sha256(user_text.encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        st = parse_state(user_text)
        ans = decide(st, rng)
        self._send(200, {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(ans)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18081
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write("mock house LLM on 127.0.0.1:%d\n" % port)
    server.serve_forever()
