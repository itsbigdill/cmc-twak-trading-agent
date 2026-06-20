# Demo video script + shot list (~3 min)

Honest walkthrough built as a small story. Tone: a builder showing you something
they're into. Warm, specific, no hype. The screen carries the receipts.

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
- Browser tab 2: bscscan attestation, https://bscscan.com/address/0x32A84F2cf8D55a8eC5414D7DC42b0D873A98AB19
- Editor open with `agent/decision.py` and `agent/risk_gate.py`.
- Record at 1080p or higher. Pre-run each command once so output is warm and there
  is no debugging on camera. If a command is slow, run it, then trim the dead time.
- Style rules: no long dashes anywhere. No superlatives. Read numbers as they are.

================================================================================

## 0:00 · Hook: the conflict (25s)
SCREEN, in order:
1. Open with a real red market shot (CMC homepage or a BTC chart in the red), 3-4s.
2. Cut to the dashboard. Let the equity chart and the GREEN "MAX DRAWDOWN 1.35%"
   tile sit in frame. Do not zoom the red return number.
3. Quick cut to the running service: in the terminal run
   `systemctl status cmc-twak-agent`  (the green "active (running)" line is the shot)

VOICEOVER:
> "Look at the market right now. It's red, people are losing money, and honestly
> this is the worst possible time to be running a trading bot. Which is exactly why
> I built one for it. Anyone's bot looks brilliant when everything's pumping. The
> real test is a month like this one, and most of them quietly blow up. So for BNB
> Hack I built an agent to survive the down months, not just ride the up ones. It
> runs on its own, reads the market every 15 minutes, and signs its own swaps on
> BSC. Let me show you."

## 0:25 · How it sees the market (35s)
SCREEN, in order:
1. Run `.venv/bin/python scripts/verify_cmc.py`  (the tool list / 12 tools is the shot)
2. Then show real signals. Easiest clean shot: tail the decision log and let one
   tick scroll by:  `tail -n 40 logs/decisions.jsonl | python3 -m json.tool` is
   messy, so instead just `tail -f logs/decisions.jsonl` for ~4s and let lines with
   RSI / MACD / EMA / regime appear, then Ctrl-C.
3. Optional overlay: point at one `"regime": "trend_down"` line.

VOICEOVER:
> "It all starts with data. Every 15 minutes it pulls from CoinMarketCap's Agent
> Hub. Momentum on each coin, so RSI, MACD, moving averages. Then the wider mood:
> Fear and Greed, Bitcoin dominance, funding. All of that collapses into a single
> number per coin, and that number is the decision. No crystal ball. Just the
> signals you'd check yourself, read consistently, without the emotion."

## 1:00 · The strategy, and the honest tradeoff (45s)
SCREEN, in order:
1. Editor: `agent/decision.py`, scroll slowly over the RotationDecider (uptrend
   holds strongest, downtrend rotates to cash).
2. Editor: `agent/risk_gate.py`, scroll over the stop / daily-pause / kill-switch.
3. Optional: cut to `config.yaml` lines for `per_position_stop_pct`,
   `daily_loss_stop_pct`, `drawdown_kill_pct` so the numbers are visible.

VOICEOVER:
> "The logic is simple, and that's on purpose. In an uptrend it holds the strongest
> coins. The moment things turn, it backs off to cash. Now the honest part. This is
> a careful agent, not a greedy one. In a straight bull run, a riskier bot beats it.
> But this contest disqualifies you at around 30% drawdown, and I cared more about
> never seeing that number than about topping a leaderboard for a day. It's spot
> only too, no shorts, no leverage, because that's what the Trust Wallet kit allows.
> And every trade has to clear a risk gate first: stop losses, a daily pause, a kill
> switch."

## 1:45 · Proof: the payoff (40s)   <-- this is where the strong number lives
SCREEN, in order:
1. Run the backtest:
   `.venv/bin/python scripts/backtest.py --policy rotation --universe core --period year`
   Let the final summary line land on screen (return, max drawdown). THIS is the
   number to hold the camera on, not the live tile.
2. Then run `.venv/bin/python -m agent.reporting` and point at the blocked-trade
   reasons (proof the decisions are logged and auditable).

VOICEOVER:
> "So did it work? Here's a backtest on a full year of real prices, and to be fair,
> a backtest is a simulation, not a guarantee. But watch this stretch. The market
> falls about 47 percent. The agent only drops around 12, and it never once touches
> the disqualification line. That's the whole idea, holding up. And every trade it
> decided to skip is logged with the reason, so you can audit its judgement instead
> of just trusting it."

## 2:25 · The three integrations, with receipts (35s)
SCREEN, in order:
1. Dashboard "sponsor stack" card (the four names), 3s.
2. x402 payment, live. Run:
   `twak x402 quote "$X402_SIGNAL_URL"`   (shows the $0.001 / USDC price option)
   then the real signed request:
   `twak x402 request "$X402_SIGNAL_URL" --max-payment 1000`
   (this signs a real ~tenth-of-a-cent payment and returns the bias signal)
3. ERC-8004 on-chain identity. Run:
   `twak erc8004 show 138200 --chain bsc`         (the on-chain identity card)
   `twak erc8004 get-metadata 138200 --key cta-perf --chain bsc`   (raw 0x value)
   decode it on camera so viewers see real numbers:
   `python3 -c "print(bytes.fromhex('PASTE_HEX_WITHOUT_0x').decode())"`
   -> {"equity":35.3,"return_pct":0.1,"trades":22}
4. Cut to the browser bscscan tab so the transaction is visibly on-chain.

VOICEOVER:
> "Three integrations, and I made each one do real work. CoinMarketCap is the eyes.
> Trust Wallet's kit signs the swaps, and it even pays for premium signals itself,
> about a tenth of a cent each, over x402. Here's one going through. And on BNB Chain
> the agent has its own ERC-8004 identity, where it writes its track record straight
> to the blockchain. There's the transaction. You can read the numbers off the
> contract without taking my word for any of it."

## 3:00 · Close: resolution (15s)
SCREEN: GitHub repo page, then the dashboard URL.
> "That's the agent. Open source, every decision logged in public, and the on-chain
> records are right there if you want to check the receipts yourself. Thanks for
> watching."

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
Give the hook a little weight, the conflict is real so let it breathe. Speak like
you are showing a friend something you are proud of, warm but never salesy. Let
terminal output sit on screen long enough to read. The honesty in the strategy and
proof sections is the point, so deliver it plainly and let it land.
