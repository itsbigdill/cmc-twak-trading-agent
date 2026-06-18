#!/usr/bin/env bash
# One-shot on-chain registration for BNB HACK Track 1.
# Prereq: agent wallet funded with a little BNB for gas (BSC mainnet).
#   compete register  -> enters the competition (records agent address)
#   erc8004 register  -> mints the agent identity NFT (special prize)
# Wallet password is read from the OS keychain (or TWAK_WALLET_PASSWORD env).
set -euo pipefail
cd "$(dirname "$0")/.."

ADDR="0x32A84F2cf8D55a8eC5414D7DC42b0D873A98AB19"

echo "== gas check =="
twak wallet balance --chain bsc --json

echo "== 1/2: competition registration (BSC) =="
twak compete register --json

echo "== 2/2: ERC-8004 identity (special prize) =="
# Embed the agent card as an inline data: URI (no hosting needed).
URI="data:application/json;base64,$(base64 < config/agent-card.json | tr -d '\n')"
twak erc8004 register --chain bsc --uri "$URI" \
  --metadata "framework=cmc-twak-bnb" --json

echo "== verify =="
twak compete status --json
echo "Done. Record the agentId above for the DoraHacks submission."
