#!/usr/bin/env bash
# Flip the agent from dry-run to LIVE trading. Run ON/AFTER the window opens
# (22 Jun) — trading before the window just wastes money.
#   - stops the service, clears dry-run state
#   - sets mode: live
#   - seeds starting cash from the real on-chain USDT balance
#   - restarts the 24/7 service
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . .env; set +a

USDT_CONTRACT="0x55d398326f99059fF775485246999027B3197955"

echo "== stopping service =="
systemctl stop cmc-twak-agent || true

echo "== clearing dry-run state =="
rm -f state/portfolio.json logs/decisions.jsonl

echo "== mode -> live (via .env, survives git pull) =="
grep -q '^AGENT_MODE=' .env && sed -i 's/^AGENT_MODE=.*/AGENT_MODE=live/' .env || echo 'AGENT_MODE=live' >> .env
export AGENT_MODE=live

echo "== reading on-chain USDT balance =="
SEED=$(twak balance --token "$USDT_CONTRACT" --chain bsc --json \
       | python3 -c "import sys,json;print(round(float(json.load(sys.stdin)['available']),2))")
echo "seed cash = \$$SEED"

echo "== seeding live state (one tick) =="
.venv/bin/python -m agent.agent --once --seed-cash "$SEED"

echo "== starting 24/7 service =="
systemctl start cmc-twak-agent
sleep 3
systemctl is-active cmc-twak-agent && echo "LIVE. watch: journalctl -u cmc-twak-agent -f"
