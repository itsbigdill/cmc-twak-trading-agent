# CMC Special Prize: #CMCAgentHub tweet ($2K, 10 winners)

Judged on **Creativity, Engagement, Usefulness**. Must: hashtag `#CMCAgentHub`,
mention the Hackathon, and be in BNB Hack. **Tag all 3 sponsor accounts** —
`@coinmarketcap` · `@bnbchain` · `@trustwallet` — an organizer confirmed they
monitor every tweet and repost the best ones.

**Format: a thread** (threads beat single tweets on engagement and let us show
more). Tweet 1 carries all the required tags. Attach the dashboard image to T1 and
the demo video to T2.

---

## Tweet 1 (what it is + required tags + dashboard image)

> I built a trading agent for BNB Hack. It reads the market every 15 minutes and
> signs its own spot swaps on BSC — no human in the loop.
>
> I'm not going to oversell it. Here's how it works, and what it can't do. 🧵
>
> @coinmarketcap #CMCAgentHub · @trustwallet · @bnbchain · BNB Hack
>
> [attach: dashboard screenshot]

## Tweet 2 (the honest design goal + proof + demo video)

> The goal wasn't max returns. It was: don't blow up.
>
> The contest disqualifies you past ~30% drawdown, so in downtrends it rotates to
> cash. In a backtest over a market that fell ~47%, it was down ~12% and never hit
> the DQ line. A simulation, not a promise — but that's the behavior I designed for.
>
> [attach: demo video]

## Tweet 3 (CMC usefulness — concrete, no secret sauce)

> Every decision starts with CoinMarketCap's Agent Hub.
>
> Per coin: RSI, MACD, moving averages.
> Market-wide: Fear & Greed, BTC dominance, funding rates.
>
> Those combine into one score per token. That's the whole input — no secret sauce.

## Tweet 4 (sponsors — each doing real work, not logos)

> Three integrations, each load-bearing rather than decorative:
> 🔹 @coinmarketcap Agent Hub — the data layer
> 🔹 @trustwallet Agent Kit — signs the swaps, and pays for premium signals via x402
> 🔹 @bnbchain ERC-8004 — an on-chain identity it writes its track record to
>
> Open source, public decision log. #CMCAgentHub #BNBHack

## Tweet 5 (on-chain track record — stated plainly, with the receipt)

> One part I haven't seen elsewhere: the agent writes its own track record on-chain.
>
> An @bnbchain ERC-8004 record it updates itself — equity, return, trade count. You
> can read it straight off the contract. No trust required, just verify.
>
> [attach: bscscan tx of the attestation]

## What it can't do (optional T6 — leaning into the honesty)

> To be straight about the limits:
> - spot only, no shorts or leverage (that's what the kit supports)
> - it trades the 149 eligible tokens, nothing off-list
> - conservative by design — in a straight bull run, riskier bots will out-return it
>
> I'd rather it survive the week than top a sprint.

---

## Required checklist (verify before posting T1)
- [ ] `#CMCAgentHub`
- [ ] tag all 3: `@coinmarketcap` `@bnbchain` `@trustwallet`
- [ ] mentions the Hackathon (BNB Hack)
- [ ] keep Twitter DMs OPEN (CMC contacts winners via DM)
- [ ] DM the tweet to CMC on Telegram too (per rules)

## Timing
Post on **go-live day (June 22)** with the agent actually trading. Live dashboard
plus real tx links give max usefulness and authenticity. Then a short follow-up
near **June 28** with final results to harvest more engagement.

## Assets
- Posting account: **@itsabigdill** (language: English)
- Dashboard screenshot: `open dashboard/index.html`, full-screen, screenshot
- Demo video: per DEMO.md
- Final reply tweet, drop the links:
  > Code + reproducible decision logs: https://github.com/DanMarteens/cmc-twak-trading-agent
  > DoraHacks BUIDL: https://dorahacks.io/buidl/45594
