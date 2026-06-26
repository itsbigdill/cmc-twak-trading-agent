"""Progressive winner protection for open spot positions.

The live bot enters many small recovery trades where the difference between a
good scalp and churn is usually a few percent.  A flat trailing stop is too
coarse here: if a position has already printed +4%, letting it fade back to
+1% is bad tournament math, while using a tiny fixed gap on a +15% runner cuts
the move too early.

This module keeps that policy explicit and testable:
  * low winners lock a small positive floor;
  * +4% winners lock around +3%;
  * larger winners get a wider but still ratcheting floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProfitLockResult:
    stop_price: float | None
    floor_pct: float | None
    peak_pnl: float
    reason: str | None


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _configured_floor_pct(lock_cfg: dict, peak_pnl: float) -> float | None:
    """Return entry-relative profit floor for the achieved peak PnL.

    ``floor_steps`` are sorted defensively so config order cannot silently make
    the lock looser.  Each step is an entry-relative floor, e.g.
    ``peak_pct=0.04, floor_pct=0.03`` means: after the executable mark has been
    at least +4%, close if it falls to +3%.
    """
    floor_pct: float | None = None
    steps = lock_cfg.get("floor_steps") or []
    parsed: list[tuple[float, float]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        peak = _float(step.get("peak_pct"), -1.0)
        floor = _float(step.get("floor_pct"), -1.0)
        if peak >= 0.0 and floor >= 0.0:
            parsed.append((peak, floor))
    for peak, floor in sorted(parsed):
        if peak_pnl >= peak:
            floor_pct = max(floor_pct or 0.0, floor)
    return floor_pct


def progressive_profit_lock(
    *,
    avg_price: float,
    peak_price: float,
    current_price: float,
    lock_cfg: dict,
) -> ProfitLockResult:
    """Compute the active profit-lock floor for a long spot position.

    Returns a stop reason only when ``current_price`` has crossed the computed
    floor.  The caller remains responsible for hard loss stops and execution.
    """
    if not lock_cfg.get("enabled") or avg_price <= 0 or peak_price <= 0 or current_price <= 0:
        return ProfitLockResult(None, None, 0.0, None)

    peak_pnl = peak_price / avg_price - 1.0
    activation = _float(lock_cfg.get("activation_pct"), 0.10)
    if peak_pnl < activation:
        return ProfitLockResult(None, None, peak_pnl, None)

    floor_pct = _configured_floor_pct(lock_cfg, peak_pnl)
    if floor_pct is None:
        floor_pct = _float(lock_cfg.get("breakeven_floor_pct"), 0.02)
    else:
        floor_pct = max(floor_pct, _float(lock_cfg.get("breakeven_floor_pct"), 0.0))

    trailing_activation = _float(lock_cfg.get("trailing_activation_pct"), 0.20)
    if peak_pnl >= trailing_activation:
        trailing_gap = _float(lock_cfg.get("trailing_gap_pct"), 0.08)
        trailing_floor_pct = (peak_price * (1.0 - trailing_gap)) / avg_price - 1.0
        floor_pct = max(floor_pct, trailing_floor_pct)

    stop_price = avg_price * (1.0 + floor_pct)
    if current_price <= stop_price:
        return ProfitLockResult(
            stop_price=stop_price,
            floor_pct=floor_pct,
            peak_pnl=peak_pnl,
            reason=(
                f"profit lock (peak={peak_pnl:.3f}, "
                f"floor_pct={floor_pct:.3f}, floor={stop_price:.8f})"
            ),
        )
    return ProfitLockResult(stop_price, floor_pct, peak_pnl, None)
