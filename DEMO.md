# Demo video script + shot list (~3 min)

Plain, honest walkthrough. Talk like you are explaining it to a friend, not
pitching. No slogans, no superlatives. The screen shows the proof; the voiceover
just explains what we are looking at.

## Framing decision (read first)
The live paper return right now is roughly flat, and that is fine. Do NOT flex the
live return. The contest resets on June 22, so today's paper % is not the scored
number, and in a red market flat is a good outcome. On the dashboard lead the eye
with the green survival stats (MAX DRAWDOWN 1.35%, DQ HEADROOM). The strong return
number comes from the BACKTEST, not the live tile.

## Pre-flight checklist (do before recording)
- Terminal font ~18pt, dark theme, wide window, clear scrollback.
- SSH into the VPS, run from `/opt/cmc-twak-agent` with env loaded:
  `cd /opt/cmc-twak-agent && set -a && . ./.env && set +a`
- Browser tab 1: dashboard, http://cmc-twak-agent.duckdns.org:8888
- Browser tab 2: bscscan, https://bscscan.com/address/0x32A84F2cf8D55a8eC5414D7DC42b0D873A98AB19
- Browser tab 3: the CoinMarketCap homepage (opener shot).
- Editor open with `agent/decision.py` and `agent/risk_gate.py`.
- Pre-run each command once so output is warm. Trim dead time in the edit.
- Style: no long dashes anywhere. Read numbers as they are.

================================================================================

## 0:00 · Opening (20s)
SCREEN: CoinMarketCap homepage, the Fear & Greed gauge on 20 and the 1Y market-cap
chart in the red (4-5s). Then the dashboard with the green MAX DRAWDOWN tile. Then
the terminal: `systemctl status cmc-twak-agent` showing active.

VOICEOVER:
> The market's been red all year. Fear and Greed is at 20, and prices are about half
> off their peak. Not exactly the moment you'd pick to let a bot trade for you.
>
> That's the problem I wanted to solve. So I built an agent that trades on its own,
> through markets like this one. It checks the market every 15 minutes and makes its
> own swaps on BSC. Let me walk you through it.

## 0:20 · How it reads the market (35s)
SCREEN: `.venv/bin/python scripts/verify_cmc.py` (tool list). Then
`tail -f logs/decisions.jsonl` for a few seconds so a tick scrolls by, Ctrl-C.

VOICEOVER:
> It starts with data from CoinMarketCap's Agent Hub. For each coin it looks at
> momentum: RSI, MACD, moving averages. Then the wider mood: Fear and Greed, Bitcoin
> dominance, funding.
>
> All of that turns into a single score per coin, and that score is what decides
> the trade.

## 0:55 · The strategy (45s)
SCREEN: `agent/decision.py`, then `agent/risk_gate.py`, scrolling slowly. Optional
`config.yaml` lines for the stop, daily loss, and kill-switch numbers.

VOICEOVER:
> The approach is straightforward. When the trend is up, it holds the strongest
> coins. When it turns, it moves to cash.
>
> It's cautious by design. In a strong bull run, a more aggressive bot would beat
> it. But this contest disqualifies you near 30% drawdown, and avoiding that
> mattered more to me than topping a leaderboard for a day.
>
> It only trades spot, no shorts or leverage, and every trade goes through a risk
> check first: stop losses, a daily pause, and a kill switch.

## 1:40 · Proof (40s)
SCREEN: run the backtest and hold on the final summary line (return, max drawdown):
`.venv/bin/python scripts/backtest.py --policy rotation --universe core --period year`
Then `.venv/bin/python -m agent.reporting` and point at the blocked-trade reasons.

VOICEOVER:
> To test it, I ran a backtest over a full year of real prices. It's a simulation,
> not a promise. But over a stretch where the market dropped about 47%, the agent
> was down around 12, and it never hit the disqualification line.
>
> And it logs the reason behind every trade it skips, so you can check its decisions
> instead of taking my word for it.

## 2:20 · The three integrations (35s)
SCREEN: dashboard "sponsor stack" card. Then in the terminal:
`twak x402 quote "$X402_SIGNAL_URL"`
`twak x402 request "$X402_SIGNAL_URL" --max-payment 1000`
`twak erc8004 show 138200 --chain bsc`
`twak erc8004 get-metadata 138200 --key cta-perf --chain bsc`
decode on camera: `python3 -c "print(bytes.fromhex('PASTE_HEX_WITHOUT_0x').decode())"`
-> {"equity":35.3,"return_pct":0.1,"trades":22}
Then cut to the bscscan tab so the transaction is visibly on-chain.

VOICEOVER:
> Three things work together here. CoinMarketCap provides the data. Trust Wallet's
> kit signs the swaps, and it even pays for premium signals on its own, about a tenth
> of a cent each, over x402.
>
> And on BNB Chain it has its own ERC-8004 identity, where it writes its track record
> on-chain. You can read those numbers straight off the contract.

## 2:55 · Close (15s)
SCREEN: GitHub repo, then the dashboard URL.
> That's the agent. It's open source, every decision is logged, and the on-chain
> records are public if you want to verify any of it. Thanks for watching.

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
Speak calmly, like you are explaining it to a colleague. Let terminal output sit on
screen long enough to read. Don't sell the numbers, just show them.
