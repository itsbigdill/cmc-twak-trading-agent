# CMC × TWAK Autonomous Trading Agent — BNB HACK Track 1

Autonomous BSC trading agent: reads **CoinMarketCap** signals → decides via an
**LLM under hard risk rules** → executes swaps/perps via **Trust Wallet Agent Kit** →
registers identity on-chain via **BNB AI Agent SDK** (ERC-8004).

Built for *BNB HACK: AI Trading Agent Edition*. Submission lock **21 Jun 12:00 UTC**,
live trading **22–28 Jun**, judged on a held-out window by returns, drawdown,
risk-adjusted performance, and rule adherence.

## Why this design wins

Track 1 ranks the **top 5 by total return** with a **hard drawdown DQ** (~30%) and a
**minimum trade count + simulated tx costs**. That shapes the optimal strategy:

| Judging criterion | What the agent does |
|---|---|
| **Returns** | Real CMC signals + regime detection; **perps allow shorting downtrends**, not just sitting in cash |
| **Drawdown** | **Layered defense**: per-position stop (cut losers fast) + daily entry pause (resets next day → recovery) + kill switch at 25% peak-to-now (close all + halt; buffer under the ~30% DQ) |
| **Risk-adjusted** | **Tournament sizing**: aggressive while healthy, size shrinks automatically as drawdown approaches the kill line |
| **Rule adherence** | Every limit enforced in one `risk_gate`; **every blocked trade is logged with a reason** — shown to judges. Min 1 trade/day guaranteed; portfolio kept deployed (>$1/hour rule) |

Pure conservatism finishes mid-pack (no prize); blowing the DQ line scores zero.
This agent maximizes return *subject to never touching the DQ line*.

### Official rules wired in (per FAQ)
- Register on-chain before the window: `twak compete register` (CLI) or `competition_register` (MCP). Contract [`0x212c61b9…29aed5`](https://bsctrace.com/address/0x212c61b9b72c95d95bf29cf032f5e5635629aed5) on BSC; on-chain deadline **25 Jun**. Then submit the agent address + strategy explainer on DoraHacks.
- Only the **149 eligible BEP-20 tokens** count (`config/eligible_tokens.txt`) — off-list trades ignored. **BTC/BTCB and BNB are NOT eligible** → signal/regime only; **USDT is eligible** (cash leg). Trade targets are `twak.token_contracts` (eligible ∩ has-contract); a config test enforces this.
- **Min 1 trade/day (7/week)**; hourly scoring; any hour starting with a **sub-$1 portfolio scores 0%** → keep capital deployed.
- Ranked by **total return**, ~30% max-drawdown DQ. Self-funded mainnet wallet. TWAK is **spot-only** → long/cash strategy (no shorting).

## Architecture

```
CMC MCP ──quotes/TA/F&G/news──► signal_engine ──score[-1..1]+regime──► decision (Claude)
                                                                          │ JSON
                                                                          ▼
   state/log ◄── executor (TWAK swap/perp) ◄──pass── risk_gate ◄──{action,size,conf}
                                                       │ fail
                                                       └──► blocked + logged
BNB SDK (ERC-8004) ──► one-time on-chain agent identity (special prize)
```

| File | Role | Spec |
|---|---|---|
| `agent/signal_engine.py` | Deterministic score + regime (judge-reproducible) | F2 |
| `agent/decision.py` | Claude structured-JSON decisions + rule-based fallback | F3 |
| `agent/risk_gate.py` | All risk checks + tournament sizing | F4 |
| `agent/executor.py` | TWAK swap/perp, slippage cap, idempotency | F5 |
| `agent/state.py` | Persistent state, positions, idempotent orders | F6 |
| `agent/reporting.py` | PnL, max drawdown, Sharpe-like, win-rate, blocks | F7 |
| `agent/agent.py` | Main loop + startup reconciliation + de-risk | — |

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# offline demo (no keys): 60 simulated ticks -> report
.venv/bin/python scripts/demo.py --ticks 60 --cash 200
.venv/bin/python -m agent.reporting

# tests (deterministic core)
.venv/bin/python -m pytest -q
```

### Going live (during the trading window)

1. `cp .env.example .env` and fill `CMC_MCP_API_KEY`, `ANTHROPIC_API_KEY`.
2. Install TWAK: `curl -fsSL https://agent-kit.trustwallet.com/install.sh | bash`,
   create the **agent wallet**, fund it with **minimal** capital on BSC.
3. Confirm CMC tool names once: `python -m agent.agent --list-cmc-tools`,
   then fix `_TOOL_NAMES` in `cmc_client.py` if they differ.
4. Confirm TWAK CLI flags in `executor.py` (`_swap_cmd` / `_perp_cmd`) against the
   installed version.
5. Set `mode: live` in `config.yaml`. First run: `--seed-cash <USD>`.
6. Run under a supervisor for 24/7 uptime (`systemd`/`pm2`) with auto-restart.

> ⚠️ Real funds on mainnet. Every limit lives in `config.yaml`; keep capital minimal.

## Rule-adherence evidence

`logs/decisions.jsonl` records the full chain per tick: `tick → signal → decision →
blocked|fill (tx_hash)`. The report prints how many trades were blocked and why —
this is the artifact for the rule-adherence score.

## Sponsor checklist

- [x] **CMC** signals drive every decision — live MCP verified (`scripts/verify_cmc.py`), real quotes/TA/F&G/dominance wired in `cmc_client.py`
- [x] **TWAK** agent-wallet execution (`executor.py`)
- [ ] **BNB SDK** ERC-8004 identity — register agent wallet + `agentId` (testnet) before lock
- [ ] On-chain registration of agent wallet address in the Track 1 contract before lock
