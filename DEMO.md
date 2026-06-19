# Demo video script (~2.5 min)

Goal: a working end-to-end agent + all three sponsors + honest results. Record
screen + voiceover. Keep it fast and natural — talk, don't read.

## 0:00 — Hook (15s)
Show the dashboard (`open dashboard/index.html`) full screen.
> "Hey! Let me show you something that trades crypto on its own. No human clicking
> buttons. It reads the market, makes the call, and signs its own swaps on-chain.
> We built it for BNB Hack."

## 0:15 — CMC Agent Hub (30s)
Run `python scripts/verify_cmc.py` → 12 tools. Then a live snapshot or a log line:
BTC RSI / MACD / EMA, Fear & Greed, BTC dominance.
> "Every decision starts at CoinMarketCap's Agent Hub. The agent reads the mood of
> the market — Fear and Greed, Bitcoin dominance — and the momentum of each coin.
> That's its eyes."

## 0:45 — Strategy + risk (40s)
Show `agent/decision.py` (RotationDecider) and `agent/risk_gate.py`.
> "The idea is simple. In an uptrend, hold the strongest coins. In a downtrend,
> step back to cash. And before any trade goes through, it passes a strict risk
> gate — stop-losses, a daily pause, and a kill switch at 15% drawdown. Staying
> alive matters more than being greedy."

## 1:25 — Proof: backtest + rule adherence (35s)
Run `python scripts/backtest.py --policy rotation --universe core --period year`.
> "Does it actually work? Same code, real prices. The market dropped 47%. We were
> down 14 — and never came close to getting disqualified."
Run `python -m agent.reporting` → point at blocked-trade reasons.
> "And every trade it refuses to make is logged with a reason. So you can check
> its homework."

## 2:00 — TWAK execution + on-chain identity (25s)
Show a live `twak swap --quote-only` quote, then `twak compete status`
(registered) and the ERC-8004 `agentId`.
> "The trades are real — spot swaps through Trust Wallet's Agent Kit. The agent is
> registered on-chain for the competition, and it has its own on-chain identity.
> All three sponsors, working as one."

## 2:25 — Close (10s)
Show the GitHub repo URL.
> "It's open source, it logs every decision, and it's been running around the
> clock. That's our agent — thanks for watching!"

---
**Delivery tips:** smile when you say the hook and the close — it carries through
the voice. Pause a beat between sections. Don't rush the numbers (47%, 14%).

**B-roll to capture:** dashboard, a tick in `journalctl`/logs, the decisions.jsonl
tail, `twak compete status` JSON, the repo.
