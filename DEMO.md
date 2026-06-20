# Demo video script (~3 min)

Goal: an honest walkthrough built as a small story, not an ad. There's a real
tension here (every bot looks great until the market drops), so use it: open with
the conflict, build through how it handles it, pay it off with proof. Tone is a
builder showing you something they're into. Warm, specific, no hype. The screen
carries the receipts: real terminal, real dashboard, real on-chain transactions.
Everything is verifiable on GitHub and bscscan, and we say so.

Style rules: no long dashes anywhere, use periods, commas, colons, or parentheses.
No superlatives. If a number is on screen, say it plainly. If it's a simulation,
call it a simulation.

## 0:00 · Hook: the conflict (25s)
Show the live market in the red (a CMC/chart shot), then cut to the dashboard and
the running service (`systemctl status` / a log tail).
> "Look at the market right now. It's red, people are losing money, and honestly
> this is the worst possible time to be running a trading bot. Which is exactly why
> I built one for it. Anyone's bot looks brilliant when everything's pumping. The
> real test is a month like this one, and most of them quietly blow up. So for BNB
> Hack I built an agent to survive the down months, not just ride the up ones. It
> runs on its own, reads the market every 15 minutes, and signs its own swaps on
> BSC. Let me show you."

## 0:20 · How it sees the market (35s)
Run `python scripts/verify_cmc.py` (tool list), then a real snapshot / log line:
RSI, MACD, EMA per token, plus Fear & Greed and BTC dominance.
> "It all starts with data. Every 15 minutes it pulls from CoinMarketCap's Agent
> Hub. Momentum on each coin, so RSI, MACD, moving averages. Then the wider mood:
> Fear and Greed, Bitcoin dominance, funding. All of that collapses into a single
> number per coin, and that number is the decision. No crystal ball. Just the
> signals you'd check yourself, read consistently, without the emotion."

## 0:55 · The strategy, and the honest tradeoff (45s)
Show `agent/decision.py` and `agent/risk_gate.py`.
> "The logic is simple, and that's on purpose. In an uptrend it holds the strongest
> coins. The moment things turn, it backs off to cash. Now the honest part. This is
> a careful agent, not a greedy one. In a straight bull run, a riskier bot beats it.
> But this contest disqualifies you at around 30% drawdown, and I cared more about
> never seeing that number than about topping a leaderboard for a day. It's spot
> only too, no shorts, no leverage, because that's what the Trust Wallet kit allows.
> And every trade has to clear a risk gate first: stop losses, a daily pause, a kill
> switch."

## 1:40 · Proof: the payoff (40s)
Run `python scripts/backtest.py --policy rotation --universe core --period year`,
then `python -m agent.reporting` to show blocked-trade reasons.
> "So did it work? Here's a backtest on a full year of real prices, and to be fair,
> a backtest is a simulation, not a guarantee. But watch this stretch. The market
> falls about 47 percent. The agent only drops around 12, and it never once touches
> the disqualification line. That's the whole idea, holding up. And every trade it
> decided to skip is logged with the reason, so you can audit its judgement instead
> of just trusting it."

## 2:20 · The three integrations, with receipts (35s)
Show the dashboard "sponsor stack" card, then a terminal: a `twak swap
--quote-only`, a `twak x402 request` returning a price-paid signal, and bscscan
open on the ERC-8004 metadata transaction with `twak erc8004 get-metadata`.
> "Three integrations, and I made each one do real work. CoinMarketCap is the eyes.
> Trust Wallet's kit signs the swaps, and it even pays for premium signals itself,
> about a tenth of a cent each, over x402. Here's one going through. And on BNB Chain
> the agent has its own ERC-8004 identity, where it writes its track record straight
> to the blockchain. There's the transaction. You can read the numbers off the
> contract without taking my word for any of it."

## 2:55 · Close: resolution (15s)
Show the GitHub repo and the dashboard URL.
> "That's the agent. Open source, every decision logged in public, and the on-chain
> records are right there if you want to check the receipts yourself. Thanks for
> watching."

---
**Delivery tips:** open with a little weight on the hook, the conflict is real so
let it breathe. Speak like you're showing a friend something you're proud of, warm
but never salesy. Let terminal output sit on screen long enough to read. The
honesty in the strategy and proof sections is the point, so deliver it plainly and
let it land.

**B-roll to capture:** dashboard (incl. the sponsor-stack card), `systemctl
status`, a tick in the logs, the decisions.jsonl tail, `twak compete status`, a
`twak x402 request` firing, the bscscan ERC-8004 transaction, the repo.
