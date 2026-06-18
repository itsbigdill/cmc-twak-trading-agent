"""
Backtest the REAL strategy on REAL historical prices — no money, no mocked alpha.

Pulls price history from TWAK, computes indicators (EMA/RSI/MACD) from that
history, and replays each bar through the *exact same* process_tick() the live
agent uses (signal_engine -> decision -> risk_gate -> simulated fill). This is
the honest test of whether the edge is in our code.

    python scripts/backtest.py --period year --cash 200

Limitations (stated honestly): Fear&Greed/news/dominance aren't in price history,
so they're held neutral — the backtest exercises the price-based core (0.80 of
the signal weight) plus the full risk/execution machinery.
"""

import argparse
import copy
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import load_config, process_tick
from agent.cmc_client import derive_ema_trend, derive_macd_state
from agent.decision import RuleBasedDecider
from agent.executor import MockExecutor
from agent.logbook import DecisionLog
from agent.reporting import max_drawdown, sharpe_like, _returns
from agent.signal_engine import Regime
from agent.state import PortfolioState
from datetime import datetime, timezone


# ----- indicators (computed from price history) -------------------------------
def ema(prices, n):
    k = 2 / (n + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def rsi(prices, n=14):
    if len(prices) <= n:
        return [50.0] * len(prices)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    out = [50.0] * len(prices)
    avg_g = sum(gains[1:n + 1]) / n
    avg_l = sum(losses[1:n + 1]) / n
    for i in range(n, len(prices)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i]) / n
            avg_l = (avg_l * (n - 1) + losses[i]) / n
        rs = (avg_g / avg_l) if avg_l > 0 else 999
        out[i] = 100 - 100 / (1 + rs)
    return out


def indicators(prices):
    e7, e30 = ema(prices, 7), ema(prices, 30)
    e12, e26 = ema(prices, 12), ema(prices, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    signal = ema(macd_line, 9)
    r = rsi(prices, 14)
    return e7, e30, macd_line, signal, r


def fetch_history(token, period, contracts):
    # BSC trade tokens need their contract; the benchmark (BTC) resolves by symbol
    ref = contracts.get(token)
    cmd = ["twak", "price", ref or token, "--history", period, "--json"]
    if ref:
        cmd += ["--chain", "bsc"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    data = json.loads(out.stdout[out.stdout.find("{"):])
    return [(h["date"], h["price"]) for h in data.get("history", [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="year")    # hour|day|week|month|year|all
    ap.add_argument("--cash", type=float, default=200.0)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = copy.deepcopy(load_config(args.config))
    cfg["risk"]["min_seconds_between_trades"] = 0   # bars are replayed fast
    tradeable = list(cfg["twak"]["token_contracts"])     # ETH, CAKE
    bench = cfg["regime"]["benchmark"]                    # BTC
    tokens = [bench] + [t for t in tradeable if t != bench]
    cfg["whitelist"] = tokens + [cfg["quote_asset"]]

    print(f"Fetching {args.period} history for {tokens} ...")
    contracts = cfg["twak"]["token_contracts"]
    series = {t: fetch_history(t, args.period, contracts) for t in tokens}
    n = min(len(v) for v in series.values())
    if n < 40:
        sys.exit(f"not enough history ({n} bars); try --period year")
    # align from the end, build per-token price list + indicators
    prices = {t: [p for _, p in series[t][-n:]] for t in tokens}
    dates = [d for d, _ in series[bench][-n:]]
    ind = {t: indicators(prices[t]) for t in tokens}

    for p in (cfg["paths"]["state_file"], "logs/backtest.jsonl"):
        if os.path.exists(p):
            os.remove(p)
    log = DecisionLog("logs/backtest.jsonl")
    state = PortfolioState(cash_usd=args.cash, initial_equity=args.cash,
                           peak_equity=args.cash, day_start_equity=args.cash)
    decider, executor = RuleBasedDecider(cfg), MockExecutor(cfg)

    warmup = 35
    for i in range(warmup, n):
        snap = {}
        for t in tokens:
            e7, e30, ml, sig, r = ind[t]
            snap[t] = {
                "price": prices[t][i],
                "rsi": r[i],
                "macd_state": derive_macd_state(
                    {"macdLine": ml[i], "signalLine": sig[i], "histogram": ml[i] - sig[i]}),
                "ema_trend": derive_ema_trend(
                    {"exponential_moving_average_7_day": e7[i],
                     "exponential_moving_average_30_day": e30[i]}),
                "fear_greed_index": 50, "btc_dominance": 54.0, "news_sentiment": 0.0,
            }
        ts = dates[i]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        bar_prices = {t: prices[t][i] for t in tokens}
        process_tick(cfg, state, snap, bar_prices, decider, executor, log,
                     now_ts=ts, date_str=dt.strftime("%Y-%m-%d"), hour=dt.hour,
                     ts_iso=dt.isoformat())

    curve = state.equity_curve
    rets = _returns(curve)
    final = curve[-1][1] if curve else args.cash
    bh = {t: prices[t][-1] / prices[t][warmup] - 1 for t in tradeable}   # buy&hold ref

    print("\n=== BACKTEST (real prices, real strategy code) ===")
    print(f"  bars:           {n - warmup}  ({args.period})")
    print(f"  equity:         ${args.cash:.0f} -> ${final:.2f}  "
          f"({(final/args.cash-1)*100:+.2f}%)")
    print(f"  max drawdown:   {max_drawdown(curve)*100:.2f}%   "
          f"(DQ line {cfg['risk']['drawdown_dq_reference_pct']*100:.0f}%)")
    print(f"  sharpe-like:    {sharpe_like(rets):.3f}")
    print(f"  trades:         {state.trade_count_total}")
    print(f"  buy&hold ref:   " + ", ".join(f"{t} {v*100:+.1f}%" for t, v in bh.items()))


if __name__ == "__main__":
    main()
