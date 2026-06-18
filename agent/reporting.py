"""
Reporting (F7) — build the metrics judges score on, from the logged artifacts.

Reads the persisted equity curve (state) and the decision log (fills/blocks) and
prints: total return, max drawdown, Sharpe-like ratio, win-rate, trade count,
and a rule-adherence summary (how many trades were blocked and why) — the last
one is our evidence for the "rule adherence" criterion.

Usage: python -m agent.reporting [--config config.yaml]
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter

import yaml


def _returns(equity_curve: list) -> list[float]:
    out = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        cur = equity_curve[i][1]
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown(equity_curve: list) -> float:
    peak = -math.inf
    mdd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return mdd


def sharpe_like(rets: list[float]) -> float:
    """Per-tick Sharpe (risk-free = 0). Not annualized — comparative only."""
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return mean / sd if sd > 0 else 0.0


def load_decisions(path: str) -> list[dict]:
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def report(cfg: dict) -> dict:
    with open(cfg["paths"]["state_file"]) as f:
        state = json.load(f)
    curve = state.get("equity_curve", [])
    rets = _returns(curve)
    rows = load_decisions(cfg["paths"]["decision_log"])

    fills = [r for r in rows if r.get("kind") == "fill"]
    blocks = [r for r in rows if r.get("kind") == "blocked"]
    block_reasons = Counter(b["reason"].split(":")[0] for b in blocks)

    init = state.get("initial_equity", 0) or 1
    last_eq = curve[-1][1] if curve else init
    total_return = last_eq / init - 1.0

    # win-rate proxy: realized pnl sign per close (mock); refined in live with tx pnl
    closes = [f for f in fills if f.get("action") in ("close", "sell")]

    metrics = {
        "initial_equity": round(init, 2),
        "final_equity": round(last_eq, 2),
        "total_return_pct": round(total_return * 100, 2),
        "max_drawdown_pct": round(max_drawdown(curve) * 100, 2),
        "dq_reference_pct": cfg["risk"]["drawdown_dq_reference_pct"] * 100,
        "sharpe_like": round(sharpe_like(rets), 3),
        "realized_pnl": round(state.get("realized_pnl", 0), 2),
        "total_trades": state.get("trade_count_total", 0),
        "min_trades_required": cfg["risk"]["min_trades_total"],
        "fills": len(fills),
        "closes": len(closes),
        "blocked_trades": len(blocks),
        "block_reasons": dict(block_reasons),
    }
    return metrics


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args(argv)
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    m = report(cfg)

    print("\n=== BNB HACK Track 1 — Agent Report ===")
    print(f"  Equity:        ${m['initial_equity']} -> ${m['final_equity']}  "
          f"({m['total_return_pct']:+.2f}%)")
    print(f"  Max drawdown:  {m['max_drawdown_pct']:.2f}%   "
          f"(DQ line {m['dq_reference_pct']:.0f}% — "
          f"{'SAFE' if m['max_drawdown_pct'] < m['dq_reference_pct'] else 'BREACH!'})")
    print(f"  Sharpe-like:   {m['sharpe_like']}")
    print(f"  Realized PnL:  ${m['realized_pnl']}")
    print(f"  Trades:        {m['total_trades']} "
          f"(min required {m['min_trades_required']} — "
          f"{'OK' if m['total_trades'] >= m['min_trades_required'] else 'BELOW MIN'})")
    print(f"  Rule adherence: {m['blocked_trades']} trades BLOCKED  {m['block_reasons']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
