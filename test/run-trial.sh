#!/usr/bin/env bash
# Local Werewolf trial: mock house LLM + env server + my agent + scorer.
# Usage: ./run-trial.sh <trial 0|1|2> [seed]
# Env vars: AGENT (path to main.py, default ../main.py)
set -u
TRIAL="${1:-0}"
KIT="$HOME/workspace/makefun-werewolf/kit/werewolf-agent-01-kit"
WORK="$HOME/workspace/makefun-werewolf/test/run-trial$TRIAL"
AGENT="${AGENT:-$HOME/workspace/makefun-werewolf/main.py}"
SEED="$(python3 -c "import json; print(json.load(open('$KIT/rules/instance.yaml'.replace('.yaml','.json')))['x'])" 2>/dev/null || true)"
# seed from instance.yaml (practice trial t)
SEED="$(grep -A2 "t: $TRIAL" "$KIT/rules/instance.yaml" | grep 'seed:' | awk '{print $2}' | tr -d '"')"
mkdir -p "$WORK/data" "$WORK/state" "$WORK/output" "$WORK/scores"
cp "$KIT/practice/trial-$TRIAL/input/env/table.json" "$WORK/data/table.json"
cp "$KIT/practice/trial-$TRIAL/reference/answer.json" "$WORK/reference-answer.json" 2>/dev/null || true
mkdir -p "$WORK/reference"
cp "$KIT/practice/trial-$TRIAL/reference/answer.json" "$WORK/reference/answer.json"
cp "$KIT/practice/trial-$TRIAL/input/env/table.json" "$WORK/reference/table.json"

# 1. mock house LLM
python3 "$HOME/workspace/makefun-werewolf/test/mock_house.py" 18081 > "$WORK/mock-house.log" 2>&1 &
MOCK_PID=$!
sleep 1
# 2. env server
MAKEFUN_ENV_DATA_DIR="$WORK/data" MAKEFUN_STATE_DIR="$WORK/state" MAKEFUN_ENV_PORT=18080 \
  MAKEFUN_HOUSE_LLM_URL="http://127.0.0.1:18081" MAKEFUN_HOUSE_TOKEN="test-token" \
  node "$KIT/program/env/server.mjs" > "$WORK/env.log" 2>&1 &
ENV_PID=$!
sleep 2
# 3. my agent
MAKEFUN_ENV_URL="http://127.0.0.1:18080" \
  MAKEFUN_PROMPT_FILE="$KIT/practice/trial-$TRIAL/input/prompt.txt" \
  MAKEFUN_TRIAL_SEED="$SEED" \
  python3 -P -s "$AGENT" > "$WORK/agent.log" 2>&1
AGENT_RC=$?
echo "agent exit: $AGENT_RC"
tail -3 "$WORK/agent.log"
# 4. stop env (SIGTERM exports state)
kill -TERM "$ENV_PID" 2>/dev/null
sleep 2
kill "$MOCK_PID" 2>/dev/null
ls "$WORK/state/export/" 2>/dev/null
# 5. score
cp "$WORK/state/export/state.json" "$WORK/output/state.json" 2>/dev/null
MAKEFUN_OUTPUT_DIR="$WORK/output" MAKEFUN_REFERENCE_DIR="$WORK/reference" MAKEFUN_SCORES_DIR="$WORK/scores" \
  node "$KIT/program/scorer/score.mjs"
echo "--- scores.json:"
cat "$WORK/scores/scores.json" 2>/dev/null
