# Demo video script + shot list (~2.5 min)

Short, substantive walkthrough. Every sentence carries a fact, a number, or a
mechanism. No slogans, no filler, no framing lines. Shorter is better as long as
it is all substance. The screen shows the proof; the voiceover names what it is.

## Framing decision (read first)
The live paper return is roughly flat right now, and that is fine. Do NOT show the
live return as a headline. The contest resets on June 22, so today's paper % is not
scored. On the dashboard lead with the green survival stats (MAX DRAWDOWN 1.35%,
DQ HEADROOM). The strong return number comes from the BACKTEST, not the live tile.

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

## 0:00 · Opening (15s)
SCREEN: CoinMarketCap homepage, Fear & Greed gauge on 20 and the 1Y market-cap
chart in the red. Then the dashboard (green MAX DRAWDOWN tile). Then the terminal:
`systemctl status cmc-twak-agent` showing active.

VOICEOVER:
> Looks like the market's been red since the start of the year. Prices are half off
> their peak. Not the best moment for a trading bot, right?
>
> Looks like we need an agent that can survive a bear market and trade its way
> through it. So that's what I built. It reads the market every 15 minutes and signs
> its own swaps on BSC.

## 0:15 · How it reads the market (30s)
SCREEN: `.venv/bin/python scripts/verify_cmc.py` (tool list). Then
`tail -f logs/decisions.jsonl` for a few seconds, Ctrl-C.

VOICEOVER:
> It pulls data from CoinMarketCap's Agent Hub. Per coin: RSI, MACD, moving
> averages. Plus Fear and Greed, Bitcoin dominance, and funding.
>
> That becomes one score per coin. The score picks the trades.

## 0:45 · The strategy (35s)
SCREEN: `agent/decision.py`, then `agent/risk_gate.py`. Optional `config.yaml`
lines for the stop, daily loss, and kill-switch numbers.

VOICEOVER:
> Trend up, it holds the strongest coins. Trend down, it moves to cash.
>
> It trades spot only. No shorts, no leverage.
>
> Every trade clears a risk gate first: a stop loss, a daily pause, a kill switch.
>
> The contest disqualifies you near 30% drawdown. That is what the gate is built to
> avoid.

## 1:20 · Proof (30s)
SCREEN: run the backtest, hold on the final summary line (return, max drawdown):
`.venv/bin/python scripts/backtest.py --policy rotation --universe core --period year`
Then `.venv/bin/python -m agent.reporting`, point at the blocked-trade reasons.

VOICEOVER:
> A backtest over one year of real prices. It is a simulation, not a promise.
>
> The market fell 47%. The agent was down 12. It never hit the disqualification line.
>
> Every skipped trade is logged with its reason.

## 1:50 · The three integrations (30s)
SCREEN: dashboard "sponsor stack" card. Then in the terminal:
`twak x402 quote "$X402_SIGNAL_URL"`
`twak x402 request "$X402_SIGNAL_URL" --max-payment 1000`
`twak erc8004 show 138200 --chain bsc`
`twak erc8004 get-metadata 138200 --key cta-perf --chain bsc`
decode: `python3 -c "print(bytes.fromhex('PASTE_HEX_WITHOUT_0x').decode())"`
-> {"equity":35.3,"return_pct":0.1,"trades":22}
Then cut to the bscscan tab.

VOICEOVER:
> Three integrations, all live. CoinMarketCap is the data.
>
> Trust Wallet signs the swaps. It also pays for premium signals over x402, a tenth
> of a cent each.
>
> On BNB Chain it has an ERC-8004 identity. It writes its track record on-chain. You
> can read it off the contract.

## 2:20 · Close (10s)
SCREEN: GitHub repo, then the dashboard URL.
> Open source. Every decision logged. The on-chain records are public. All the links
> are in the description.

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
Speak calmly. One short sentence at a time. Let terminal output sit on screen long
enough to read. Don't sell the numbers, just show them.
