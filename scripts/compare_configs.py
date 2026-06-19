"""
Compare risk/decision postures on the SAME fetched history.

Fetches price history once per period, then replays several config variants
through the exact live process_tick() and tabulates return / drawdown / trades.
Lets us pick a posture on evidence, not vibes.

    python scripts/compare_configs.py --period month
    python scripts/compare_configs.py --period year
"""

import argparse
import copy
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import load_config, process_tick
from agent.cmc_client import derive_ema_trend, derive_macd_state
from agent.indicators import indicators
from agent.decision import build_decider
from agent.executor import MockExecutor
from agent.logbook import DecisionLog
from agent.reporting import max_drawdown, sharpe_like, _returns
from agent.state import PortfolioState
from scripts.backtest import fetch_history


VARIANTS = {
    "Defensive(old)": {
        "risk": {"max_position_pct": 0.20, "max_concentration_pct": 0.40,
                 "min_confidence": 0.55, "per_position_stop_pct": 0.04,
                 "daily_loss_stop_pct": 0.08, "drawdown_kill_pct": 0.15},
        "signal": {"entry_threshold": 0.30},
        "decision": {"rotation_top_k": 5, "rotation_downtrend_topk": 0,
                     "rotation_downtrend_min_momentum": 0.25},
    },
    "Aggressive(cur)": {
        "risk": {"max_position_pct": 0.30, "max_concentration_pct": 0.50,
                 "min_confidence": 0.50, "per_position_stop_pct": 0.10,
                 "daily_loss_stop_pct": 0.12, "drawdown_kill_pct": 0.24},
        "signal": {"entry_threshold": 0.20},
        "decision": {"rotation_top_k": 4, "rotation_downtrend_topk": 2,
                     "rotation_downtrend_min_momentum": 0.15},
    },
    "Balanced": {
        "risk": {"max_position_pct": 0.24, "max_concentration_pct": 0.45,
                 "min_confidence": 0.52, "per_position_stop_pct": 0.08,
                 "daily_loss_stop_pct": 0.10, "drawdown_kill_pct": 0.20},
        "signal": {"entry_threshold": 0.25},
        "decision": {"rotation_top_k": 5, "rotation_downtrend_topk": 1,
                     "rotation_downtrend_min_momentum": 0.20},
    },
    "Bal+chopguard": {  # balanced but only trade in clear trends (skip downtrend churn)
        "risk": {"max_position_pct": 0.24, "max_concentration_pct": 0.45,
                 "min_confidence": 0.52, "per_position_stop_pct": 0.08,
                 "daily_loss_stop_pct": 0.10, "drawdown_kill_pct": 0.20},
        "signal": {"entry_threshold": 0.28},
        "decision": {"rotation_top_k": 5, "rotation_downtrend_topk": 0,
                     "rotation_downtrend_min_momentum": 0.25},
    },
    "Bull-rider": {  # cash in downtrend + tight stop (defend flat/bear) + BIG uptrend
                     # positions + high entry bar (anti-churn) => ride clean bulls only
        "risk": {"max_position_pct": 0.28, "max_concentration_pct": 0.50,
                 "min_confidence": 0.52, "per_position_stop_pct": 0.05,
                 "daily_loss_stop_pct": 0.10, "drawdown_kill_pct": 0.20},
        "signal": {"entry_threshold": 0.30},
        "decision": {"rotation_top_k": 4, "rotation_downtrend_topk": 0,
                     "rotation_downtrend_min_momentum": 0.25},
    },
}


def replay(cfg, tokens, bench, prices, dates, ind, n, cash, warmup=35):
    for p in (cfg["paths"]["state_file"], "logs/_cmp.jsonl"):
        if os.path.exists(p):
            os.remove(p)
    log = DecisionLog("logs/_cmp.jsonl")
    state = PortfolioState(cash_usd=cash, initial_equity=cash,
                           peak_equity=cash, day_start_equity=cash)
    decider, executor = build_decider(cfg), MockExecutor(cfg)
    for i in range(warmup, n):
        snap = {}
        for t in tokens:
            e7, e30, ml, sig, r = ind[t]
            snap[t] = {
                "price": prices[t][i], "rsi": r[i],
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
    final = curve[-1][1] if curve else cash
    return {"ret": (final / cash - 1) * 100, "dd": max_drawdown(curve) * 100,
            "sharpe": sharpe_like(_returns(curve)), "trades": state.trade_count_total}


def apply(cfg, ov):
    c = copy.deepcopy(cfg)
    for sect, vals in ov.items():
        c.setdefault(sect, {}).update(vals)
    c["risk"]["min_seconds_between_trades"] = 0
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="month")
    ap.add_argument("--cash", type=float, default=200.0)
    args = ap.parse_args()

    base = load_config("config.yaml")
    bench = base["regime"]["benchmark"]
    contracts = base["twak"]["token_contracts"]
    tokens0 = [bench] + [t for t in contracts if t != bench]

    print(f"Fetching {args.period} history for {len(tokens0)} tokens ...")
    series = {}
    for t in tokens0:
        h = fetch_history(t, args.period, contracts)
        if len(h) >= 40:
            series[t] = h
        time.sleep(0.4)
    tokens = [t for t in tokens0 if t in series]
    n = min(len(v) for v in series.values())
    prices = {t: [p for _, p in series[t][-n:]] for t in tokens}
    dates = [d for d, _ in series[bench][-n:]]
    ind = {t: indicators(prices[t]) for t in tokens}
    trade_syms = [t for t in tokens if t != bench]
    bh = sum(prices[t][-1] / prices[t][35] - 1 for t in trade_syms) / len(trade_syms) * 100

    print(f"\n=== {args.period.upper()}  ({n-35} bars, {len(trade_syms)} tokens, "
          f"equal-weight buy&hold {bh:+.2f}%) ===")
    print(f"{'config':<18}{'return':>9}{'maxDD':>8}{'sharpe':>8}{'trades':>8}")
    for name, ov in VARIANTS.items():
        cfg = apply(base, ov)
        cfg["twak"]["token_contracts"] = {t: contracts[t] for t in tokens if t in contracts}
        cfg["whitelist"] = tokens + [cfg["quote_asset"]]
        r = replay(cfg, tokens, bench, prices, dates, ind, n, args.cash)
        flag = "  <-- DQ!" if r["dd"] >= base["risk"]["drawdown_dq_reference_pct"] * 100 else ""
        print(f"{name:<18}{r['ret']:>8.2f}%{r['dd']:>7.2f}%{r['sharpe']:>8.3f}{r['trades']:>8}{flag}")


if __name__ == "__main__":
    main()
