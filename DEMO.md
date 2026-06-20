# Demo video script + shot list (~3 min)

Honest walkthrough built as a small story. Tone: a builder showing you something
they're into. Warm, specific, no hype. The screen carries the receipts.
Voiceover is written one short sentence per line, on purpose. Read it that way,
with small pauses. Easy to follow, easy to teleprompt.

## Framing decision (read first)
The live paper return right now is roughly flat-to-slightly-negative, and that is
fine. We do NOT flex the live return number. Two reasons it still works:
- The contest resets on June 22, so today's paper % is not the scored number.
- The agent's own regime reads `trend_down`. In a red market, flat is the win.
So on the dashboard we lead the eye with the GREEN survival stats (MAX DRAWDOWN
1.35%, DQ HEADROOM), and the impressive return number comes from the BACKTEST,
not the live tile.

## Pre-flight checklist (do before recording)
- Terminal font bumped to ~18pt, dark theme, wide window. Clear scrollback.
- SSH into the VPS in that terminal: everything runs from `/opt/cmc-twak-agent`
  with the env loaded: `cd /opt/cmc-twak-agent && set -a && . ./.env && set +a`
- Browser tab 1: the dashboard, http://cmc-twak-agent.duckdns.org:8888
- Browser tab 2: bscscan, https://bscscan.com/address/0x32A84F2cf8D55a8eC5414D7DC42b0D873A98AB19
- Browser tab 3: the CoinMarketCap homepage (for the opener shot).
- Editor open with `agent/decision.py` and `agent/risk_gate.py`.
- Record at 1080p or higher. Pre-run each command once so output is warm and there
  is no debugging on camera. If a command is slow, run it, then trim the dead time.
- Style rules: no long dashes anywhere. No superlatives. Read numbers as they are.

================================================================================

## 0:00 · Hook: the conflict (25s)
SCREEN, in order:
1. Open on the CoinMarketCap homepage: the Fear & Greed gauge on 20 and the 1Y
   market-cap chart in the red. 4-5s. This anchors the claim AND it is the data
   the agent reads.
2. Cut to the dashboard. Let the equity chart and the GREEN "MAX DRAWDOWN 1.35%"
   tile sit in frame. Do not zoom the red return number.
3. Quick cut to the terminal: `systemctl status cmc-twak-agent` (the green
   "active (running)" line is the shot).

VOICEOVER:
> Since the start of this year, everything's been red.
>
> Looks like we're heading into a bear market.
>
> Fear and Greed is down at 20.
>
> The market's off about half from its peak.
>
> So you'd think this is the worst time to run a trading bot.
>
> But this is exactly when you want one that knows how to trade it.
>
> Any bot looks smart when everything's pumping.
>
> The real test is a market like this.
>
> Most of them blow up.
>
> So I built one to survive the down months. Not just ride the up ones.
>
> It runs on its own.
>
> It reads the market every 15 minutes.
>
> It signs its own swaps on BSC.
>
> Let me show you.

## 0:25 · How it sees the market (35s)
SCREEN, in order:
1. Run `.venv/bin/python scripts/verify_cmc.py` (the tool list is the shot).
2. Then `tail -f logs/decisions.jsonl` for ~4s, let a tick with RSI / MACD / EMA /
   regime scroll by, then Ctrl-C.
3. Optional: point at a `"regime": "trend_down"` line.

VOICEOVER:
> It all starts with data.
>
> Every 15 minutes it reads CoinMarketCap's Agent Hub.
>
> Per coin: RSI, MACD, moving averages.
>
> Then the bigger picture. Fear and Greed. Bitcoin dominance. Funding.
>
> All of it becomes one score per coin.
>
> That score is the decision.
>
> No crystal ball.
>
> Just the signals you'd check yourself, read without the emotion.

## 1:00 · The strategy, and the honest tradeoff (45s)
SCREEN, in order:
1. Editor: `agent/decision.py`, scroll slowly over the RotationDecider.
2. Editor: `agent/risk_gate.py`, scroll over the stop / daily-pause / kill-switch.
3. Optional: `config.yaml` lines for the stop, daily loss, and kill numbers.

VOICEOVER:
> The strategy is simple, on purpose.
>
> In an uptrend, it holds the strongest coins.
>
> When things turn, it moves to cash.
>
> Now the honest part.
>
> This is a careful agent, not a greedy one.
>
> In a full bull run, a riskier bot beats it.
>
> But this contest disqualifies you at around 30% drawdown.
>
> I cared more about never seeing that number.
>
> It's spot only too. No shorts. No leverage.
>
> That's what the Trust Wallet kit allows.
>
> And every trade clears a risk gate first.
>
> Stop losses. A daily pause. A kill switch.

## 1:45 · Proof: the payoff (40s)   <-- this is where the strong number lives
SCREEN, in order:
1. Run the backtest:
   `.venv/bin/python scripts/backtest.py --policy rotation --universe core --period year`
   Hold the camera on the final summary line (return, max drawdown).
2. Then `.venv/bin/python -m agent.reporting` and point at the blocked-trade reasons.

VOICEOVER:
> So does it work?
>
> This is a backtest on a full year of real prices.
>
> A backtest is a simulation, not a guarantee.
>
> But watch this part.
>
> The market falls about 47 percent.
>
> The agent only drops around 12.
>
> It never touches the disqualification line.
>
> That's the whole idea, holding up.
>
> And every trade it skips is logged with a reason.
>
> So you can audit it, not just trust it.

## 2:25 · The three integrations, with receipts (35s)
SCREEN, in order:
1. Dashboard "sponsor stack" card (the four names), 3s.
2. x402 payment, live:
   `twak x402 quote "$X402_SIGNAL_URL"`
   `twak x402 request "$X402_SIGNAL_URL" --max-payment 1000`
3. ERC-8004 on-chain identity:
   `twak erc8004 show 138200 --chain bsc`
   `twak erc8004 get-metadata 138200 --key cta-perf --chain bsc`
   decode on camera:
   `python3 -c "print(bytes.fromhex('PASTE_HEX_WITHOUT_0x').decode())"`
   -> {"equity":35.3,"return_pct":0.1,"trades":22}
4. Cut to the bscscan tab so the transaction is visibly on-chain.

VOICEOVER:
> Three integrations. Each one does real work.
>
> CoinMarketCap is the eyes.
>
> Trust Wallet's kit signs the swaps.
>
> It even pays for premium signals itself. About a tenth of a cent each. Over x402.
>
> Here's one going through.
>
> And on BNB Chain it has its own ERC-8004 identity.
>
> It writes its track record straight to the blockchain.
>
> There's the transaction.
>
> You can read the numbers off the contract yourself.

## 3:00 · Close: resolution (15s)
SCREEN: GitHub repo page, then the dashboard URL.
> That's the agent.
>
> It's open source.
>
> Every decision is logged in public.
>
> The on-chain records are there if you want to check.
>
> Thanks for watching.

================================================================================

## Exact commands, copy-paste (all from /opt/cmc-twak-agent, env loaded)
```
cd /opt/cmc-twak-agent && set -a && . ./.env && set +a
systemctl status cmc-twak-agent
.venv/bin/python scripts/verify_cmc.py
tail -f logs/decisions.jsonl            # Ctrl-C after a tick scrolls by
.venv/bin/python scripts/backtest.py --policy rotation --universe core --period year
.venv/bin/python -m agent.reporting
twak x402 quote "$X402_SIGNAL_URL"
twak x402 request "$X402_SIGNAL_URL" --max-payment 1000
twak erc8004 show 138200 --chain bsc
twak erc8004 get-metadata 138200 --key cta-perf --chain bsc
```
Dashboard:  http://cmc-twak-agent.duckdns.org:8888
bscscan:    https://bscscan.com/address/0x32A84F2cf8D55a8eC5414D7DC42b0D873A98AB19

## Delivery tips
Give the hook a little weight, the conflict is real so let it breathe. One short
sentence at a time, small pauses between them. Speak like you are showing a friend
something you are proud of, warm but never salesy. Let terminal output sit on
screen long enough to read.
