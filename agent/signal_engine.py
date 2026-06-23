"""
Deterministic signal engine (F2).

Turns raw CMC indicators into a transparent score in [-1, +1] per token, plus a
global market regime. Pure functions, no side effects, no network — so judges can
replay any tick from the logged inputs and get the exact same score.

Design choices that matter for judging:
  * Few magic constants (anti-overfit; held-out window).
  * Score sign is directional: +1 = strong long bias, -1 = strong short bias.
    Negative scores are actionable via perps (short), not just "don't buy".
  * Regime gates activity: in chop we dampen scores hard so the agent trades
    less, which is what protects drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"


# MACD state -> contribution in [-1, 1]
_MACD_MAP = {
    "bullish_cross": 1.0,
    "bullish": 0.5,
    "neutral": 0.0,
    "bearish": -0.5,
    "bearish_cross": -1.0,
}
# EMA trend -> contribution in [-1, 1]
_EMA_MAP = {"up": 1.0, "flat": 0.0, "down": -1.0}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def rsi_component(rsi: float, oversold: float, overbought: float) -> float:
    """Oversold -> long bias (+), overbought -> short bias (-).

    Linear through the midpoint (50). At/below `oversold` saturates to +1,
    at/above `overbought` saturates to -1.
    """
    mid = (oversold + overbought) / 2.0          # 50 for 30/70
    half_span = (overbought - oversold) / 2.0    # 20 for 30/70
    return _clip((mid - rsi) / half_span)


def fear_greed_component(fg: float) -> float:
    """Mild contrarian tilt: extreme fear -> small long, extreme greed -> small short."""
    return _clip((50.0 - fg) / 50.0)


def funding_component(funding_pct: float, scale: float) -> float:
    """CMC derivatives funding rate, read contrarian: crowded longs (high positive
    funding) -> caution (negative); negative funding -> opportunity. `scale` is the
    funding % that maps to a full ±1. Absent funding (0.0) -> 0 contribution, so
    the price-only backtest is unaffected."""
    if not scale:
        return 0.0
    return _clip(-funding_pct / scale)


def dominance_component(dom: float, ref: float, scale: float) -> float:
    """CMC BTC dominance regime tilt: BTC dominating (dom > ref) is risk-off for alts
    (negative); falling dominance is risk-on (positive). 0 at the neutral ref, so the
    price-only backtest (dom == ref) is unaffected."""
    if not scale:
        return 0.0
    return _clip((ref - dom) / scale)


@dataclass
class TokenSignal:
    token: str
    score: float
    regime: Regime
    components: dict[str, float]
    actionable: bool          # |score| beyond entry threshold and regime allows


def detect_regime(snapshot: Mapping[str, Mapping], cfg: Mapping) -> Regime:
    """Global regime from the benchmark asset (BTC) trend + fear/greed extremes.

    Kept deliberately simple: the benchmark's EMA trend is the primary driver.
    """
    bench = cfg["regime"]["benchmark"]
    bench_data = snapshot.get(bench, {})
    ema = bench_data.get("ema_trend", "flat")
    fg = bench_data.get("fear_greed_index", 50)

    if ema == "up":
        return Regime.TREND_UP
    if ema == "down":
        return Regime.TREND_DOWN
    # Flat benchmark: only a fear/greed extreme breaks the tie, else chop.
    if fg <= cfg["regime"]["fear_extreme_low"]:
        return Regime.TREND_UP        # capitulation -> mean-revert long bias
    if fg >= cfg["regime"]["fear_extreme_high"]:
        return Regime.TREND_DOWN      # euphoria -> fade
    return Regime.CHOP


def score_token(token: str, data: Mapping, regime: Regime, cfg: Mapping) -> TokenSignal:
    w = cfg["signal"]["weights"]
    comps = {
        "rsi": rsi_component(
            data.get("rsi", 50.0),
            cfg["signal"]["rsi_oversold"],
            cfg["signal"]["rsi_overbought"],
        ),
        "macd": _MACD_MAP.get(data.get("macd_state", "neutral"), 0.0),
        "ema": _EMA_MAP.get(data.get("ema_trend", "flat"), 0.0),
        "fear_greed": fear_greed_component(data.get("fear_greed_index", 50.0)),
        "news": _clip(float(data.get("news_sentiment", 0.0))),
        "funding": funding_component(float(data.get("funding_rate", 0.0)),
                                     cfg["signal"].get("funding_scale", 0.05)),
        "dominance": dominance_component(
            float(data.get("btc_dominance", cfg["signal"].get("dominance_ref", 54.0))),
            cfg["signal"].get("dominance_ref", 54.0), cfg["signal"].get("dominance_scale", 12.0)),
        "x402": _clip(max(float(data.get("x402_bias", 0.0)),
                          float(data.get("x402_token_score", 0.0)))),
        # CoinMarketCap Pro enrichment: cross-check executable TWAK momentum
        # against CMC quote momentum, volume, daily TA, holder concentration and
        # token-specific news. 0 means unavailable/neutral, so the agent degrades
        # safely when CMC is slow or the token has no resolved id.
        "cmc": _clip(float(data.get("cmc_score", 0.0))),
    }
    weight_total = sum(abs(float(v)) for v in w.values()) or 1.0
    raw = sum(comps[k] * w[k] for k in w) / weight_total

    # Regime gating: dampen hard in chop (fewer, lower-conviction trades).
    if regime is Regime.CHOP:
        raw *= cfg["signal"]["chop_dampen"]

    score = _clip(raw)
    threshold = cfg["signal"]["entry_threshold"]
    actionable = abs(score) >= threshold and regime is not Regime.CHOP

    return TokenSignal(
        token=token,
        score=round(score, 4),
        regime=regime,
        components={k: round(v, 4) for k, v in comps.items()},
        actionable=actionable,
    )


def score_universe(snapshot: Mapping[str, Mapping], cfg: Mapping) -> dict[str, TokenSignal]:
    """Score every whitelisted token. Returns {token: TokenSignal}."""
    regime = detect_regime(snapshot, cfg)
    out: dict[str, TokenSignal] = {}
    for token in cfg["whitelist"]:
        if token == cfg["quote_asset"]:
            continue
        if token in snapshot:
            out[token] = score_token(token, snapshot[token], regime, cfg)
    return out
