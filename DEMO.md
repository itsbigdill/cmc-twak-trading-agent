# Demo video script (~2.5 min)

Goal: a working end-to-end agent + all three sponsors + honest results. Record
screen + voiceover. Same voice as the X thread (SOCIAL.md) — first person, short
human sentences, talk don't read. Each beat echoes a tweet so the video and the
thread feel like one piece.

## 0:00 — Hook (15s) · mirrors Tweet 1
Show the dashboard (`open dashboard/index.html`) full screen.
> "I built a bot to trade my crypto so I'd stop checking charts at 3am. It reads
> the market, makes its own calls, and signs its own swaps. I don't touch it. And
> when everything dumps? It does the one thing I never could — nothing. This is CTA."

## 0:15 — CMC Agent Hub (30s) · mirrors Tweet 3
Run `python scripts/verify_cmc.py` → 12 tools. Then a live snapshot / log line:
BTC RSI / MACD / EMA, Fear & Greed, BTC dominance.
> "No more 3am chart watching. No more panic selling at the bottom. Every 15
> minutes it reads the market for me — Fear and Greed, Bitcoin dominance, momentum
> on every coin. CoinMarketCap's Agent Hub is its eyes."

## 0:45 — Strategy + risk (40s) · mirrors Tweet 2 (the setup)
Show `agent/decision.py` (RotationDecider) and `agent/risk_gate.py`.
> "Here's what nobody admits. Most bots print in a bull run, then blow up the
> second the market turns. So I built this one backwards. In an uptrend it holds
> the strongest coins. In a downtrend it steps back to cash. And every trade
> passes a strict risk gate — stop-losses, a daily pause, a kill switch. Staying
> alive matters more than being greedy."

## 1:25 — Proof: it survives the crash (35s) · mirrors Tweet 2 (the proof)
Run `python scripts/backtest.py --policy rotation --universe core --period year`,
then `python -m agent.reporting` → point at blocked-trade reasons.
> "Does it actually work? Same code, real prices. We ran it through a real crash —
> the market fell 47%, and CTA was down around 12. Never came close to getting
> disqualified. Surviving the dip is the whole game. And every trade it refuses to
> make is logged with a reason, so you can check its homework."

## 2:00 — Three sponsors, all real (30s) · mirrors Tweet 4
Show the dashboard "sponsor stack" card, then a terminal: a `twak swap
--quote-only` quote, a `twak x402 request` firing (the $0.001 payment + the bias),
and bscscan open on the ERC-8004 attestation tx with `twak erc8004 get-metadata`.
> "One agent, three sponsors — and each one does real work. CoinMarketCap is its
> eyes. Trust Wallet's Agent Kit signs its own swaps, and even pays for premium
> data itself over x402. And on BNB Chain it has a real identity."

## 2:20 — The wild part (15s) · mirrors Tweet 5
Hold on the bscscan attestation tx.
> "And this is the wild part. It writes its own track record to the blockchain.
> Not a screenshot — an on-chain record it updates itself. Anyone can verify how it
> actually did. An AI trader with a real, on-chain track record."

## 2:35 — Close (10s)
Show the GitHub repo URL.
> "It's open source, it logs every decision, and it's been running around the
> clock. That's CTA — thanks for watching!"

---
**Delivery tips:** smile on the hook and the close — it carries through the voice.
Pause a beat between sections. Don't rush the numbers (47%, 12%). Say "the wild
part" and "the blockchain" slowly — those are the lines that land.

**B-roll to capture:** dashboard (esp. the sponsor-stack card), a tick in
`journalctl`/logs, the decisions.jsonl tail, `twak compete status` JSON, a
`twak x402 request` firing, the bscscan ERC-8004 attestation tx, the repo.
