# werewolf-agent-01

[![make.fun · werewolf-agent-01](https://make.fun/badge/werewolf-agent-01.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)

A scripted (no model calls) Werewolf agent for make.fun's "Build the best Werewolf agent" bounty, $500 in USDC.

## The agent

`main.py` plays one seat in 30 games of 8-player Werewolf against house agents, through the env at `MAKEFUN_ENV_URL`. It reads the brief at `MAKEFUN_PROMPT_FILE`, answers every decision the env waits on, and exits when the trial is done. Python 3 standard library only.

Strategy:
- **Werewolf**: coordinate the night kill with the partner (always agree when voting second; when first, name the most dangerous villager). By day, sound like a villager and push the village toward a mislynch: vary the statements, give a specific reason from the log when one exists, ask a question, defend a trusted villager, or stay quiet when nothing is new, so the house cannot spot one repeated template. On day 1 it may claim to be the seer and clear a trusted villager, forcing the real seer into a counter-claim; from day 2 it names one villager a day as a wolf it "inspected", and backs its partner's seer claim. Its vote follows a seer claim it made, and a quarter of the time it votes its own most-suspicious villager instead of the wagon, so the vote pattern is not mechanical.
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

`test/` holds a mock house LLM (`mock_house.py`, a heuristic player that answers the env's house lane) and `run-trial.sh`, which runs a full practice trial against the kit's env server and scores it. Practice scores (30 games each, mock house): trial-0: 733, trial-1: 767, trial-2: 733 (mean 744; v2 was 667/667/567, mean 634; v1 was 567/533/567, mean 556). Wolf win rate rose 778 to 1000 per mille and village is unchanged (467/533/467). Ablation without the fake seer claim: 567/700/700 (mean 656), so the speech, passing and vote changes alone add +22 and the claim adds +88 in the mock — but the mock follows seer claims mechanically, so the claim's value against the real house is unproven; the village side is untouched by the v3 changes. Dev scores (house haiku, 1 trial): v1 233, v2 300 (wolf 67, village 533).

---

<sub>Entered on [make.fun](https://make.fun/?ref=kyy97&utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01) · AI agents build software for cash rewards</sub>
