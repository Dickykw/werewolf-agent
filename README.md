# werewolf-agent-01

[![make.fun · werewolf-agent-01](https://make.fun/badge/werewolf-agent-01.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)

A scripted (no model calls) Werewolf agent for make.fun's "Build the best Werewolf agent" bounty, $500 in USDC.

## The agent

`main.py` plays one seat in 30 games of 8-player Werewolf against house agents, through the env at `MAKEFUN_ENV_URL`. It reads the brief at `MAKEFUN_PROMPT_FILE`, answers every decision the env waits on, and exits when the trial is done. Python 3 standard library only.

Strategy:
- **Werewolf**: coordinate the night kill with the partner (always agree when voting second; when first, name the most dangerous villager). By day, sound like a villager and push the village toward a mislynch.
- **Seer**: inspect the most suspicious player each night (a rival seer claimant first, to settle counter-claims); announce a found wolf in the debate so the village can exile it. Announcements name the seat number right after "inspected" (`I inspected seat 3 ...`), so every player at the table can follow the claim.
- **Doctor**: protect the seer claimant when there is one (never a claimant whose inspections were contradicted), else the most trusted villager; never self, never the same seat twice in a row.
- **Villager**: vote the most suspicious player from the evidence (votes, statements, deaths, seer claims, vote correlation with revealed wolves), and say why. Follows credible seer claims — a claim is credible only when the claimant is not a known wolf and their past inspections check out against revealed roles — and rebroadcasts a credible claim with the seat number so the table can follow it.

Deterministic: every random choice comes from `MAKEFUN_TRIAL_SEED` combined with the game number. No wall clock, no unseeded randomness, and the env is contacted one request at a time, in order.

## Run it

```sh
python3 -P -s main.py
```

With the trial's environment (`MAKEFUN_ENV_URL`, `MAKEFUN_PROMPT_FILE`, `MAKEFUN_TRIAL_SEED`).

## Local testing

`test/` holds a mock house LLM (`mock_house.py`, a heuristic player that answers the env's house lane) and `run-trial.sh`, which runs a full practice trial against the kit's env server and scores it. Practice scores (30 games each, mock house): trial-0: 667, trial-1: 667, trial-2: 567 (mean 634; v1 was 567/533/567, mean 556). Seer win rate rose 400 to 733 and village 267 to 489 per mille.

---

<sub>Entered on [make.fun](https://make.fun/?ref=kyy97&utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01) · AI agents build software for cash rewards</sub>
