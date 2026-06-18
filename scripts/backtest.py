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
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import load_config, process_tick
from agent.cmc_client import derive_ema_trend, derive_macd_state
from agent.decision import build_decider
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


def fetch_history(token, period, contracts, retries=2):
    # BSC trade tokens need their contract; the benchmark (BTC) resolves by symbol
    ref = contracts.get(token)
    cmd = ["twak", "price", ref or token, "--history", period, "--json"]
    if ref:
        cmd += ["--chain", "bsc"]
    for _ in range(retries + 1):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            data = json.loads(out.stdout[out.stdout.find("{"):])
            hist = [(h["date"], h["price"]) for h in data.get("history", [])]
            if hist:
                return hist
        except Exception:
            pass
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="year")    # hour|day|week|month|year|all
    ap.add_argument("--cash", type=float, default=200.0)
    ap.add_argument("--policy", choices=["rotation", "threshold"], default=None)
    ap.add_argument("--universe", choices=["core", "resolved"], default="core")
    ap.add_argument("--max", type=int, default=20, help="max tokens for resolved universe")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = copy.deepcopy(load_config(args.config))
    cfg["risk"]["min_seconds_between_trades"] = 0   # bars are replayed fast
    if args.policy:
        cfg.setdefault("decision", {})["policy"] = args.policy

    bench = cfg["regime"]["benchmark"]                    # BTC
    if args.universe == "resolved":
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "config", "bsc_contracts.json")) as f:
            res = json.load(f)
        pairs = [(s, v["address"]) for s, v in res.items()
                 if not v.get("stable") and not v.get("ambiguous")][: args.max]
        cfg["twak"]["token_contracts"] = dict(pairs)
    tradeable = list(cfg["twak"]["token_contracts"])
    tokens = [bench] + [t for t in tradeable if t != bench]
    cfg["whitelist"] = tokens + [cfg["quote_asset"]]

    print(f"Policy={cfg['decision']['policy']} universe={args.universe} "
          f"({len(tradeable)} tokens). Fetching {args.period} history ...")
    contracts = cfg["twak"]["token_contracts"]
    series = {}
    for t in tokens:
        h = fetch_history(t, args.period, contracts)
        if len(h) >= 40:
            series[t] = h
        time.sleep(0.4)                              # be gentle on the data API
    if bench not in series:
        sys.exit(f"benchmark {bench} history unavailable (throttled?); retry")
    tokens = [t for t in tokens if t in series]
    cfg["twak"]["token_contracts"] = {t: contracts[t] for t in tokens if t in contracts}
    cfg["whitelist"] = tokens + [cfg["quote_asset"]]
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
    decider, executor = build_decider(cfg), MockExecutor(cfg)

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
    trade_syms = [t for t in tokens if t != bench]
    bh_vals = [prices[t][-1] / prices[t][warmup] - 1 for t in trade_syms]
    bh_avg = sum(bh_vals) / len(bh_vals) if bh_vals else 0.0   # equal-weight buy&hold

    # --- emit dashboard data (our equity vs equal-weight buy&hold, per bar) ---
    from collections import OrderedDict
    by_iso = OrderedDict()
    for iso, val in curve:
        by_iso[iso] = val                       # last mark per bar timestamp wins
    ours_series = list(by_iso.values())
    dseries, bench_series = [], []
    for i in range(warmup, n):
        r = sum(prices[t][i] / prices[t][warmup] for t in trade_syms) / len(trade_syms)
        bench_series.append(round(args.cash * r, 4))
        dseries.append(datetime.fromtimestamp(dates[i], tz=timezone.utc).strftime("%Y-%m-%d"))
    rows = [json.loads(l) for l in open("logs/backtest.jsonl")] if os.path.exists("logs/backtest.jsonl") else []
    blocks = [r for r in rows if r.get("kind") == "blocked"]
    from collections import Counter
    result = {
        "generated": dseries[-1] if dseries else "",
        "policy": cfg["decision"]["policy"], "universe": args.universe,
        "tokens": trade_syms, "period": args.period,
        "dates": dseries, "equity": [round(x, 4) for x in ours_series], "benchmark": bench_series,
        "kpis": {
            "initial": args.cash, "final": round(final, 2),
            "total_return_pct": round((final / args.cash - 1) * 100, 2),
            "max_drawdown_pct": round(max_drawdown(curve) * 100, 2),
            "dq_pct": cfg["risk"]["drawdown_dq_reference_pct"] * 100,
            "sharpe_like": round(sharpe_like(rets), 3),
            "trades": state.trade_count_total,
            "buyhold_pct": round(bh_avg * 100, 2),
        },
        "positions": {t: round(p.qty, 6) for t, p in state.positions.items()},
        "cash": round(state.cash_usd, 2),
        "blocked": len(blocks),
        "block_reasons": dict(Counter(b["reason"].split(":")[0] for b in blocks)),
        "fills": [r for r in rows if r.get("kind") == "fill"][-8:],
    }
    with open("logs/backtest_result.json", "w") as f:
        json.dump(result, f, indent=2)
    print("  -> logs/backtest_result.json (dashboard data)")

    print("\n=== BACKTEST (real prices, real strategy code) ===")
    print(f"  bars:           {n - warmup}  ({args.period})")
    print(f"  equity:         ${args.cash:.0f} -> ${final:.2f}  "
          f"({(final/args.cash-1)*100:+.2f}%)")
    print(f"  max drawdown:   {max_drawdown(curve)*100:.2f}%   "
          f"(DQ line {cfg['risk']['drawdown_dq_reference_pct']*100:.0f}%)")
    print(f"  sharpe-like:    {sharpe_like(rets):.3f}")
    print(f"  trades:         {state.trade_count_total}")
    print(f"  buy&hold ref:   equal-weight {bh_avg*100:+.2f}%  "
          f"({len(trade_syms)} tokens)")


if __name__ == "__main__":
    main()
