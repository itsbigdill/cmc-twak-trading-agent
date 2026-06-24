"""
Decision layer (F3).

The LLM weighs already-computed signals and returns a STRUCTURED decision. It
never computes prices. We use the system prompt from the spec (3.1) and validate
the JSON schema; malformed output is rejected and falls back to the rule-based
decider so the live loop never stalls on a bad LLM response.

Decider interface: decide(snapshot, signals, portfolio, risk_limits) -> list[dict]
each: {token, action, size_pct, confidence, rationale}
where action in {buy, sell, short, close, hold}.
"""

from __future__ import annotations

import json
import os

from . import llm
from .signal_engine import Regime, TokenSignal

SYSTEM_PROMPT = """\
You are the decision component of an autonomous trading agent on BNB Smart Chain.
You do NOT execute trades or compute prices — you weigh already-computed signals
and return a structured decision.

RULES:
1. Respect risk_limits. NEVER propose a size_pct that violates max_position_pct.
2. If daily_loss_remaining_pct is near zero, only propose hold/close.
3. This is a rank-by-return tournament with a hard drawdown DQ: be decisive when
   the portfolio is healthy, but de-risk immediately near the line. Never all-in.
4. size_pct is a fraction of cash_usd, not of total_equity.
5. SPOT-ONLY execution (no perps/shorts/leverage). Allowed actions: buy, sell,
   hold, close. In a downtrend use "close"/"sell" (move to cash), NOT "short".
6. confidence < 0.55 → action = "hold".
7. Always give a short rationale, IN ENGLISH, citing the concrete signals.

OUTPUT — EXACTLY this JSON, no markdown:
{"decisions":[{"token":"CAKE","action":"buy|sell|hold|close","size_pct":0.0,
"confidence":0.0,"rationale":"..."}],"portfolio_note":"..."}
"""

_VALID_ACTIONS = {"buy", "sell", "short", "hold", "close", "trim"}


def runtime_validated_token(cfg: dict, token: str, snapshot: dict | None = None) -> bool:
    """True when a token has passed the dynamic executable round-trip probe.

    `deny_buy` is intentionally not always permanent. Some names (notably ZETA)
    were quarantined because a prior sell/route failed, not because the symbol is
    intrinsically malicious. A quarantined token can be re-enabled only after the
    UniverseManager records a successful buy->sell validation with acceptable
    round-trip loss.
    """
    meta = dict((cfg.get("universe_runtime") or {}).get(token) or {})
    if snapshot and isinstance(snapshot.get(token), dict):
        # Snapshot values are current-tick data; universe_runtime is the durable
        # validation cache.  Merge them so a tick can use the freshest probe
        # without allowing missing snapshot fields to erase validation metadata.
        meta.update({k: v for k, v in snapshot[token].items() if v is not None})
    if not meta:
        return False
    try:
        loss = float(meta.get("round_trip_loss_pct", 999.0))
    except (TypeError, ValueError):
        return False
    max_loss = float(cfg.get("execution", {}).get(
        "max_round_trip_loss_pct",
        cfg.get("universe", {}).get("max_round_trip_loss_pct", 3.0),
    ))
    return (
        loss <= max_loss
        and str(meta.get("risk_level", "")).lower() != "high"
        and int(meta.get("history_bars", 0) or 0) >= int(cfg.get("universe", {}).get("min_history_bars", 0))
    )


def executable_validated_token(cfg: dict, token: str) -> bool:
    return runtime_validated_token(cfg, token)


def tradeable_buy_tokens(cfg: dict) -> set[str]:
    deny = set(cfg["twak"].get("deny_buy", []))
    # Quarantine is lifted only for freshly execution-validated names.
    deny = {t for t in deny if not executable_validated_token(cfg, t)}
    return set(cfg["twak"]["token_contracts"]) - deny


def _validate(decisions: list) -> list[dict]:
    out = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        if d.get("action") not in _VALID_ACTIONS:
            continue
        d["size_pct"] = max(0.0, min(1.0, float(d.get("size_pct", 0.0))))
        d["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.0))))
        d["token"] = str(d.get("token", ""))
        d["rationale"] = str(d.get("rationale", ""))
        out.append(d)
    return out


def build_snapshot_payload(snapshot, signals: dict[str, TokenSignal], portfolio, risk_limits) -> dict:
    return {
        "tokens": {
            t: {
                "signal_score": s.score,
                "regime": s.regime.value,
                "components": s.components,
                **{k: snapshot.get(t, {}).get(k) for k in
                   ("rsi", "macd_state", "ema_trend", "fear_greed_index",
                    "btc_dominance", "news_sentiment", "cmc_id", "cmc_rank",
                    "cmc_pct_1h", "cmc_pct_24h", "cmc_pct_7d",
                    "cmc_volume_24h", "cmc_volume_change_24h",
                    "cmc_score", "cmc_rsi14", "cmc_macd_state",
                    "cmc_ema_trend", "cmc_top10_holder_pct",
                    "token_news_sentiment", "cmc_ambiguous")},
                "current_position": portfolio["positions"].get(t, 0.0),
            }
            for t, s in signals.items()
        },
        "portfolio": portfolio,
        "risk_limits": risk_limits,
    }


# --- Rule-based fallback (also the offline decider) ----------------------------
class RuleBasedDecider:
    """Maps signals straight to decisions. Deterministic, no network.

    Used in dry-run and as the safety net if the LLM call fails live.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def decide(self, snapshot, signals: dict[str, TokenSignal], portfolio, risk_limits):
        decisions = []
        for t, s in signals.items():
            held = portfolio["positions"].get(t, 0.0)
            if not s.actionable:
                # exit on signal decay if we hold something
                if held:
                    decisions.append(_dec(t, "close", 0.0, 0.6, f"signal decayed (score={s.score})"))
                continue
            conf = min(0.95, 0.5 + abs(s.score) / 2)
            if s.score > 0:
                decisions.append(_dec(t, "buy", min(0.3, abs(s.score)), conf,
                                      f"long bias score={s.score} regime={s.regime.value}"))
            elif s.score < 0 and self.cfg["risk"]["perps_enabled"]:
                decisions.append(_dec(t, "short", min(0.25, abs(s.score)), conf,
                                      f"short bias score={s.score} regime={s.regime.value}"))
            elif held:
                decisions.append(_dec(t, "close", 0.0, conf, "bearish signal, no shorting"))
        return decisions


# --- Rotation decider (cross-sectional momentum) -------------------------------
class RotationDecider:
    """Relative-strength rotation across the tradeable universe.

    The edge most threshold bots miss: instead of waiting for absolute setups
    per token, always hold the STRONGEST names by cross-sectional momentum, and
    rotate to cash only in a clear risk-off regime. This captures upside in
    trends, guarantees participation (trade cadence), and keeps the risk moat
    (cash in downturns + the same stops/sizing downstream).

      TREND_UP   -> hold top-K tokens with positive momentum
      TREND_DOWN -> defensive: hold only the few STRONGEST relative-strength names
                    (high momentum bucking the market), else cash
      CHOP       -> hold current positions (no churn, no forced cash)
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        d = cfg.get("decision", {})
        self.k = d.get("rotation_top_k", 3)
        self.min_mom = d.get("rotation_min_momentum", 0.05)
        # Counter-trend: in a downtrend, still ride the strongest relative-strength
        # names (a pure-cash agent scores ~0% in a rank-by-return contest), but
        # fewer of them and only if momentum is strong.
        self.down_k = d.get("rotation_downtrend_topk", 2)
        self.down_min_mom = d.get("rotation_downtrend_min_momentum", 0.2)
        self.tradeable = set(cfg["twak"]["token_contracts"])
        # Hysteresis: after rotating OUT of a name, don't rotate back IN for this many
        # HOURS (time-based, so it is consistent across live 15-min ticks and coarse
        # backtest bars). Kills the buy->sell->buy thrash on names whose momentum hovers
        # at the entry boundary (the main source of tx-cost churn). 0 = disabled.
        self.reentry_cooldown_sec = d.get("rotation_reentry_cooldown_hours", 0) * 3600
        self.rotation_hurdle = float(d.get("rotation_score_hurdle", 0.12))
        self.target_gross = float(d.get("target_gross_exposure_pct", 0.60))
        self.top5_gross = float(d.get("top5_gross_exposure_pct", 0.20))
        self.max_rank_mark_divergence = float(d.get("top5_max_mark_divergence_pct", 10.0))
        entry_cfg = d.get("entry_filter", {}) or {}
        self.entry_filter_enabled = bool(entry_cfg.get("enabled", True))
        self.min_entry_quality_down = float(entry_cfg.get("min_quality_downtrend", 0.03))
        self.min_entry_quality_up = float(entry_cfg.get("min_quality_uptrend", 0.02))
        self.min_entry_r6_down = float(entry_cfg.get("min_return_6h_downtrend", 0.0))
        self.max_entry_r6_down = float(entry_cfg.get("max_return_6h_downtrend", 0.08))
        self.max_entry_cmc_1h_down = float(entry_cfg.get("max_cmc_pct_1h_downtrend", 0.03))
        self.max_entry_cmc_24h_down = float(entry_cfg.get("max_cmc_pct_24h_downtrend", 0.18))
        self.max_entry_cmc_7d_down = float(entry_cfg.get("max_cmc_pct_7d_downtrend", 0.45))
        self.max_entry_distance_high_down = float(entry_cfg.get(
            "max_distance_from_48h_high_downtrend", -0.005))
        self.min_entry_volume_change_down = float(entry_cfg.get(
            "min_volume_change_24h_downtrend", -0.25))
        self.hot_volume_min_change_down = float(entry_cfg.get(
            "hot_volume_min_change_24h_downtrend", -0.10))
        self.pullback_exception_enabled = bool(entry_cfg.get(
            "pullback_exception_enabled", False))
        self.pullback_gross = float(entry_cfg.get("pullback_exposure_pct", 0.40))
        self.pullback_min_score = float(entry_cfg.get("pullback_min_score", 0.32))
        self.pullback_min_quality_down = float(entry_cfg.get(
            "pullback_min_quality_downtrend", 0.20))
        self.pullback_min_c1_down = float(entry_cfg.get(
            "pullback_min_cmc_pct_1h_downtrend", -0.12))
        self.pullback_max_c1_down = float(entry_cfg.get(
            "pullback_max_cmc_pct_1h_downtrend", -0.03))
        self.pullback_min_r6_down = float(entry_cfg.get(
            "pullback_min_return_6h_downtrend", -0.02))
        self.pullback_max_r6_down = float(entry_cfg.get(
            "pullback_max_return_6h_downtrend", 0.04))
        self.pullback_max_c24_down = float(entry_cfg.get(
            "pullback_max_cmc_pct_24h_downtrend", self.max_entry_cmc_24h_down))
        self.pullback_max_c7_down = float(entry_cfg.get(
            "pullback_max_cmc_pct_7d_downtrend", self.max_entry_cmc_7d_down))
        self.pullback_min_x402 = float(entry_cfg.get("pullback_min_x402", 0.25))
        self.pullback_min_cmc = float(entry_cfg.get("pullback_min_cmc", 0.80))
        self.pullback_max_round_trip = float(entry_cfg.get(
            "pullback_max_round_trip_loss_pct", 2.0))
        self.pullback_max_risk = float(entry_cfg.get(
            "pullback_max_token_risk_score", 30.0))
        ds = d.get("dynamic_sizing", {}) or {}
        self.dynamic_sizing = bool(ds.get("enabled", False))
        self.ds_low_score = float(ds.get("low_score", self.down_min_mom))
        self.ds_mid_score = float(ds.get("mid_score", max(self.down_min_mom, 0.32)))
        self.ds_high_score = float(ds.get("high_score", max(self.down_min_mom, 0.38)))
        self.ds_low_gross = float(ds.get("low_exposure_pct", min(self.target_gross, 0.30)))
        self.ds_mid_gross = float(ds.get("mid_exposure_pct", min(self.target_gross, 0.40)))
        self.ds_high_gross = float(ds.get("high_exposure_pct", self.target_gross))
        self.ds_high_conviction = bool(ds.get("high_conviction_enabled", False))
        self.ds_hc_gross = float(ds.get("high_conviction_exposure_pct", self.ds_high_gross))
        self.ds_hc_min_score = float(ds.get("high_conviction_min_score", self.ds_mid_score))
        self.ds_hc_min_x402 = float(ds.get("high_conviction_min_x402", 0.25))
        self.ds_hc_min_cmc = float(ds.get("high_conviction_min_cmc", 0.80))
        self.ds_hc_min_quality = float(ds.get("high_conviction_min_quality", 0.25))
        self.ds_hc_max_round_trip = float(ds.get("high_conviction_max_round_trip_loss_pct", 2.5))
        self.ds_hc_max_risk = float(ds.get("high_conviction_max_token_risk_score", 30.0))
        self.ds_hc_min_volume = float(ds.get("high_conviction_min_volume_24h_usd", 5_000_000))
        self.ds_hc_catchup_rank_above = int(ds.get("high_conviction_catchup_rank_above", 5))
        self.ds_risk_adjusted = bool(ds.get("risk_adjusted_enabled", False))
        self.ds_medium_risk_threshold = float(ds.get("medium_risk_threshold", 30.0))
        self.ds_medium_risk_gross_cap = float(ds.get("medium_risk_gross_cap", 0.15))
        self.ds_weak_cmc_threshold = float(ds.get("weak_cmc_threshold", 0.40))
        self.ds_weak_cmc_gross_cap = float(ds.get("weak_cmc_gross_cap", 0.20))
        self.ds_stress_dd = float(ds.get("stress_drawdown_pct", 0.18))
        self.ds_stress_gross = float(ds.get("stress_exposure_pct", min(self.target_gross, 0.25)))
        exit_cfg = d.get("held_exit", {}) or {}
        self.held_exit_enabled = bool(exit_cfg.get("enabled", True))
        self.held_exit_floor_down = float(exit_cfg.get(
            "floor_score_downtrend", max(0.0, self.down_min_mom - 0.04)))
        self.held_exit_floor_up = float(exit_cfg.get(
            "floor_score_uptrend", max(0.0, self.min_mom - 0.05)))
        self.held_min_quality_down = float(exit_cfg.get("min_quality_downtrend", 0.0))
        self.held_min_return_6h_down = float(exit_cfg.get("min_return_6h_downtrend", -0.015))
        self.held_min_hold_sec_down = float(exit_cfg.get("min_hold_seconds_downtrend", 0.0))
        self.held_stale_loss_pct = float(exit_cfg.get("stale_loss_pct", -0.006))
        self.held_stale_loss_min_r6 = float(exit_cfg.get("stale_loss_min_return_6h", -0.005))
        self.micro_profit_take_pct = float(exit_cfg.get("micro_profit_take_pct", 0.015))
        self.micro_profit_sell_fraction = float(exit_cfg.get("micro_profit_sell_fraction", 0.45))
        self.min_micro_profit_sell_usd = float(exit_cfg.get("min_micro_profit_sell_usd", 1.0))
        self._now = 0.0                      # set by process_tick each tick (now_ts)
        self._exited_at: dict[str, float] = {}

    def _needs_catchup(self, risk_limits: dict) -> bool:
        rank = risk_limits.get("leaderboard_rank")
        lb_ret = risk_limits.get("leaderboard_return_pct")
        exec_ret = float(risk_limits.get("executable_return_pct") or 0.0)
        try:
            rank_i = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank_i = None
        try:
            lb_ret_f = float(lb_ret) if lb_ret is not None else exec_ret
        except (TypeError, ValueError):
            lb_ret_f = exec_ret
        return (rank_i is None or rank_i > self.ds_hc_catchup_rank_above
                or min(exec_ret, lb_ret_f) < 0.0)

    def _high_conviction_target(self, token: str, signals: dict[str, TokenSignal],
                                snapshot: dict, quality: dict[str, float],
                                risk_limits: dict) -> bool:
        """LAB-like tournament setup: validated/fresh elsewhere, strong enough to size up."""
        if not self.ds_high_conviction or token not in signals or not self._needs_catchup(risk_limits):
            return False
        s = signals[token]
        d = snapshot.get(token, {})
        return (
            float(s.score) >= self.ds_hc_min_score
            and float(d.get("x402_token_score", 0.0) or 0.0) >= self.ds_hc_min_x402
            and float(d.get("cmc_score", 0.0) or 0.0) >= self.ds_hc_min_cmc
            and float(quality.get(token, -1.0)) >= self.ds_hc_min_quality
            and float(d.get("round_trip_loss_pct", 100.0) or 100.0) <= self.ds_hc_max_round_trip
            and float(d.get("token_risk_score", 100.0) or 100.0) <= self.ds_hc_max_risk
            and float(d.get("cmc_volume_24h", 0.0) or 0.0) >= self.ds_hc_min_volume
            and not self._pullback_exception(s, snapshot, quality)
        )

    def _pullback_exception(self, signal: TokenSignal, snapshot: dict,
                            quality: dict[str, float]) -> bool:
        """Allow a controlled re-entry after a real pullback, not at the vertical top.

        This is intentionally narrower than the normal entry path: it may relax the
        24h/7d anti-chase caps, but only when the short-term candle has actually
        cooled off and the route friction/risk leave room for a positive exit.
        """
        if not self.pullback_exception_enabled or signal.regime is not Regime.TREND_DOWN:
            return False
        d = snapshot.get(signal.token, {})
        c1 = float(d.get("cmc_pct_1h", 0.0) or 0.0)
        c24 = float(d.get("cmc_pct_24h", 0.0) or 0.0)
        c7 = float(d.get("cmc_pct_7d", 0.0) or 0.0)
        r6 = float(d.get("return_6h", 0.0) or 0.0)
        return (
            float(signal.score) >= self.pullback_min_score
            and float(quality.get(signal.token, -1.0)) >= self.pullback_min_quality_down
            and self.pullback_min_c1_down <= c1 <= self.pullback_max_c1_down
            and self.pullback_min_r6_down <= r6 <= self.pullback_max_r6_down
            and c24 <= self.pullback_max_c24_down
            and c7 <= self.pullback_max_c7_down
            and float(d.get("x402_token_score", 0.0) or 0.0) >= self.pullback_min_x402
            and float(d.get("cmc_score", 0.0) or 0.0) >= self.pullback_min_cmc
            and float(d.get("round_trip_loss_pct", 100.0) or 100.0) <= self.pullback_max_round_trip
            and float(d.get("token_risk_score", 100.0) or 100.0) <= self.pullback_max_risk
        )

    def _catchup_gross(self, targets: list[str], signals: dict[str, TokenSignal],
                       risk_limits: dict, snapshot: dict | None = None,
                       quality: dict[str, float] | None = None) -> float:
        """Score-scaled comeback exposure.

        The bot must participate while behind, but a borderline downtrend signal
        should not get the same 55% gross exposure as a clean breakout.  Hard
        execution/risk guards still run after this sizing step.
        """
        if not self.dynamic_sizing or not targets:
            return self.target_gross
        best_score = max(float(signals[t].score) for t in targets if t in signals)
        if best_score >= self.ds_high_score:
            gross = self.ds_high_gross
        elif best_score >= self.ds_mid_score:
            gross = self.ds_mid_gross
        else:
            gross = self.ds_low_gross

        snapshot = snapshot or {}
        quality = quality or {}
        pullback_targets = [t for t in targets
                            if t in signals and self._pullback_exception(signals[t], snapshot, quality)]
        high_conviction_targets = [t for t in targets
                                   if self._high_conviction_target(t, signals, snapshot,
                                                                   quality, risk_limits)]
        if high_conviction_targets:
            gross = max(gross, self.ds_hc_gross)
        elif pullback_targets:
            gross = max(gross, self.pullback_gross)

        if self.ds_risk_adjusted and not high_conviction_targets:
            caps = []
            for t in targets:
                d = snapshot.get(t, {})
                risk = float(d.get("token_risk_score", 100.0) or 100.0)
                cmc = float(d.get("cmc_score", 0.0) or 0.0)
                if risk > self.ds_medium_risk_threshold:
                    caps.append(self.ds_medium_risk_gross_cap)
                if cmc < self.ds_weak_cmc_threshold:
                    caps.append(self.ds_weak_cmc_gross_cap)
            if caps:
                gross = min(gross, min(caps))

        dd_vals = []
        for key in ("leaderboard_drawdown_pct", "current_drawdown_pct"):
            v = risk_limits.get(key)
            if v is None:
                continue
            try:
                x = abs(float(v))
            except (TypeError, ValueError):
                continue
            dd_vals.append(x / 100.0 if x > 1.0 else x)
        if dd_vals and max(dd_vals) >= self.ds_stress_dd:
            gross = min(gross, self.ds_stress_gross)
        return min(self.target_gross, max(0.0, gross))

    @staticmethod
    def _relative_rank(candidates: list[TokenSignal], snapshot: dict, field: str) -> dict[str, float]:
        """Map a numeric snapshot field to [-1, 1] cross-sectional rank scores."""
        vals = {s.token: float(snapshot.get(s.token, {}).get(field, 0.0)) for s in candidates}
        if not vals or max(vals.values()) - min(vals.values()) < 1e-12:
            return {s.token: 0.0 for s in candidates}
        ordered = sorted(candidates, key=lambda s: vals[s.token])
        if len(ordered) <= 1:
            return {s.token: 0.0 for s in ordered}
        return {s.token: 2.0 * i / (len(ordered) - 1) - 1.0
                for i, s in enumerate(ordered)}

    def _quality_scores(self, candidates: list[TokenSignal], snapshot: dict) -> dict[str, float]:
        """Cost/risk/extension-aware ranking score used for rotation targets."""
        rel6 = self._relative_rank(candidates, snapshot, "return_6h")
        rel24 = self._relative_rank(candidates, snapshot, "return_24h")
        quality: dict[str, float] = {}
        for s in candidates:
            d = snapshot.get(s.token, {})
            vol_adj = max(-1.0, min(1.0, float(d.get("vol_adjusted_return", 0.0)) / 3.0))
            vol_chg = max(-1.0, min(1.0, float(d.get("cmc_volume_change_24h", 0.0) or 0.0) / 1.5))
            spread = max(0.0, float(d.get("round_trip_loss_pct", 0.0))) / 100.0
            risk = max(0.0, float(d.get("token_risk_score", 0.0))) / 100.0
            r24 = float(d.get("return_24h", 0.0))
            cmc = max(-1.0, min(1.0, float(d.get("cmc_score", 0.0))))
            cmc_vol = float(d.get("cmc_volume_24h", 0.0) or 0.0)
            cmc_rank = int(d.get("cmc_rank", 0) or 0)
            whale = max(0.0, float(d.get("cmc_top10_holder_pct", 0.0) or 0.0))
            c24 = float(d.get("cmc_pct_24h", 0.0) or 0.0)
            c7 = float(d.get("cmc_pct_7d", 0.0) or 0.0)
            near_high = float(d.get("distance_from_48h_high", -1.0)) > -0.01
            cmc_late = max(0.0, c24 - 0.20) + max(0.0, c7 - 0.50) * 0.5
            chase_penalty = (max(0.0, r24 - 0.15) + cmc_late) * (1.5 if near_high else 0.5)
            liquidity_penalty = 0.0
            if cmc_vol and cmc_vol < float(self.cfg.get("cmc", {}).get("min_volume_24h_usd", 500_000)):
                liquidity_penalty = 0.08
            rank_penalty = 0.04 if cmc_rank and cmc_rank > 1000 else 0.0
            holder_penalty = max(0.0, whale - 0.85) * 0.35
            ambiguous_penalty = 0.05 if d.get("cmc_ambiguous") else 0.0
            quality[s.token] = (0.35 * s.score + 0.20 * rel6[s.token]
                                + 0.25 * rel24[s.token] + 0.15 * vol_adj
                                + 0.18 * cmc + 0.08 * vol_chg
                                - 0.75 * spread - 0.10 * risk - chase_penalty
                                - liquidity_penalty - rank_penalty - holder_penalty
                                - ambiguous_penalty)
        return quality

    def _held_floor(self, regime: Regime) -> float:
        return self.held_exit_floor_down if regime is Regime.TREND_DOWN else self.held_exit_floor_up

    def _entry_classification(self, signal: TokenSignal, regime: Regime,
                              snapshot: dict, quality: dict[str, float]) -> tuple[bool, str]:
        """Return whether a new entry is fresh enough and why."""
        if not self.entry_filter_enabled:
            return True, "entry_filter_disabled"
        d = snapshot.get(signal.token, {})
        q = quality.get(signal.token, -1.0)
        if regime is Regime.TREND_DOWN:
            r6 = float(d.get("return_6h", 0.0) or 0.0)
            c1 = float(d.get("cmc_pct_1h", 0.0) or 0.0)
            c24 = float(d.get("cmc_pct_24h", 0.0) or 0.0)
            c7 = float(d.get("cmc_pct_7d", 0.0) or 0.0)
            vol_chg = float(d.get("cmc_volume_change_24h", 0.0) or 0.0)
            dist_high = float(d.get("distance_from_48h_high", -1.0) or -1.0)
            if q < self.min_entry_quality_down:
                return False, f"low_quality:{q:.3f}"
            if self._pullback_exception(signal, snapshot, quality):
                return True, "validated_pullback"
            if r6 < self.min_entry_r6_down or r6 > self.max_entry_r6_down:
                return False, f"bad_6h:{r6:.3f}"
            if c1 > self.max_entry_cmc_1h_down:
                return False, f"late_hot_1h:{c1:.3f}"
            if c24 > self.max_entry_cmc_24h_down:
                return False, f"late_hot_24h:{c24:.3f}"
            if c7 > self.max_entry_cmc_7d_down:
                return False, f"late_hot_7d:{c7:.3f}"
            if dist_high > self.max_entry_distance_high_down and r6 > 0:
                return False, f"near_48h_high:{dist_high:.3f}"
            if vol_chg < self.min_entry_volume_change_down:
                return False, f"weak_volume:{vol_chg:.3f}"
            if (c1 > self.max_entry_cmc_1h_down * 0.70
                    or c24 > self.max_entry_cmc_24h_down * 0.70) \
                    and vol_chg < self.hot_volume_min_change_down:
                return False, f"hot_without_volume:{vol_chg:.3f}"
            if r6 >= 0.02 and c24 <= self.max_entry_cmc_24h_down * 0.70:
                return True, "fresh_bounce"
            return True, "continuation"
        if q < self.min_entry_quality_up:
            return False, f"low_quality:{q:.3f}"
        return True, "trend_up_candidate"

    @staticmethod
    def _held_pnl(token: str, portfolio: dict) -> float:
        avg = float(portfolio.get("avg_prices", {}).get(token, 0.0) or 0.0)
        value = float(portfolio.get("position_values", {}).get(token, 0.0) or 0.0)
        qty = float(portfolio.get("positions", {}).get(token, 0.0) or 0.0)
        if avg <= 0.0 or qty <= 0.0:
            return 0.0
        return value / (qty * avg) - 1.0

    def _held_exit_reason(self, signal: TokenSignal, regime: Regime, snapshot: dict,
                          quality: dict[str, float], portfolio: dict) -> str | None:
        if not self.held_exit_enabled:
            return None
        token = signal.token
        floor = self._held_floor(regime)
        if signal.score < floor:
            return f"health exit: score {signal.score:.3f} < floor {floor:.3f}"
        if regime is Regime.TREND_DOWN:
            d = snapshot.get(token, {})
            q = quality.get(token, 0.0)
            r6 = float(d.get("return_6h", 0.0) or 0.0)
            pnl = self._held_pnl(token, portfolio)
            opened_ts = float(portfolio.get("position_opened_ts", {}).get(token, 0.0) or 0.0)
            age_sec = self._now - opened_ts if opened_ts > 0 and self._now > 0 else float("inf")
            if q < self.held_min_quality_down:
                return f"health exit: quality {q:.3f} < {self.held_min_quality_down:.3f}"
            if r6 < self.held_min_return_6h_down and age_sec >= self.held_min_hold_sec_down:
                return f"health exit: 6h momentum {r6:.3f} < {self.held_min_return_6h_down:.3f}"
            if (pnl <= self.held_stale_loss_pct and r6 < self.held_stale_loss_min_r6
                    and age_sec >= self.held_min_hold_sec_down):
                return f"health exit: stale loss pnl={pnl:.3f}, r6={r6:.3f}"
        return None

    def decide(self, snapshot, signals: dict[str, TokenSignal], portfolio, risk_limits):
        buyable = tradeable_buy_tokens(self.cfg)
        held = {t for t, q in portfolio["positions"].items() if q > 0}
        # Buy candidates must be in the execution-validated buy universe.  Held
        # tokens are also considered so the strategy can make an explicit
        # hold/trim/close decision even if a name later leaves deny_buy or the
        # dynamic universe.
        cand = [s for s in signals.values() if s.token in buyable or s.token in held]
        if not cand:
            return []
        regime = cand[0].regime
        held = {t for t in held if t in {s.token for s in cand}}

        if regime is Regime.CHOP:
            return []                          # hold current; stops still run upstream

        quality = self._quality_scores(cand, snapshot)
        ranked = sorted(cand, key=lambda s: quality[s.token], reverse=True)
        confirmation = self.cfg.get("decision", {}).get("signal_confirmation", {})
        immediate = float(confirmation.get("immediate_score", 0.40))
        required_ticks = int(confirmation.get("required_ticks", 2))
        streaks = risk_limits.get("signal_streaks", {})
        validated = {s.token for s in cand
                     if runtime_validated_token(self.cfg, s.token, snapshot)}

        def confirmed(s, threshold):
            return (s.token in held or
                    (s.score >= immediate) or
                    (s.score > threshold and int(streaks.get(s.token, 0)) >= required_ticks))

        def fresh_entry(s) -> bool:
            return self._entry_classification(s, regime, snapshot, quality)[0]

        def held_healthy(s) -> bool:
            return self._held_exit_reason(s, regime, snapshot, quality, portfolio) is None

        def eligible_target(s, threshold):
            if s.token in held:
                # Held-token health rule: do not keep a decayed position just
                # because no new token is compelling.  This is strategy, not a
                # manual call: score below the configured floor exits to cash.
                return held_healthy(s)
            # New entries/rotate-ins require durable execution validation before
            # they can become targets.  High CMC momentum alone cannot bypass it.
            return (s.token in validated and s.score > threshold
                    and confirmed(s, threshold)
                    and fresh_entry(s))

        if regime is Regime.TREND_DOWN:         # only the strongest counter-trend names
            eligible = [s.token for s in ranked if eligible_target(s, self.down_min_mom)]
            limit = self.down_k
        else:
            eligible = [s.token for s in ranked if eligible_target(s, self.min_mom)]
            limit = self.k
        targets = eligible[:limit]

        # Hysteresis based on opportunity cost: keep a valid held name unless a
        # challenger is materially better after spread/risk penalties.
        for token in held:
            if token not in eligible or token in targets:
                continue
            held_signal = signals.get(token)
            if held_signal is None or not held_healthy(held_signal):
                continue
            if len(targets) < limit:
                targets.append(token)
                continue
            weakest = min(targets, key=lambda t: quality[t])
            if quality[weakest] - quality[token] < self.rotation_hurdle:
                targets[targets.index(weakest)] = token

        decisions = []
        for t in held:                         # exit names no longer targeted
            if t not in targets:
                self._exited_at[t] = self._now   # arm the re-entry cooldown
                held_signal = signals.get(t)
                reason = (self._held_exit_reason(held_signal, regime, snapshot, quality, portfolio)
                          if held_signal else None)
                decisions.append(_dec(t, "close", 0.0, 0.78 if reason else 0.7,
                                      reason or f"rotate out ({regime.value})"))
        rank = risk_limits.get("leaderboard_rank")
        lb_ret = risk_limits.get("leaderboard_return_pct")
        exec_ret = float(risk_limits.get("executable_return_pct") or 0.0)
        mark_gap = abs(float(lb_ret) - exec_ret) if lb_ret is not None else float("inf")
        top5_active = bool(rank and rank <= 5 and exec_ret >= 0
                           and mark_gap <= self.max_rank_mark_divergence)
        gross = (self.top5_gross if top5_active
                 else self._catchup_gross(targets, signals, risk_limits, snapshot, quality))
        target_value = portfolio["total_equity_usd"] * gross / max(len(targets), 1)
        cash = max(portfolio["cash_usd"], 0.0)
        min_rebalance = float(self.cfg.get("decision", {}).get("min_rebalance_usd", 1.0))
        for token in targets:
            current = portfolio.get("position_values", {}).get(token, 0.0)
            excess = current - target_value
            pnl = self._held_pnl(token, portfolio)
            if (token in held and pnl >= self.micro_profit_take_pct
                    and current * self.micro_profit_sell_fraction >= self.min_micro_profit_sell_usd):
                decisions.append({"token": token, "action": "trim", "size_pct": 0.0,
                                  "size_usd": round(current * self.micro_profit_sell_fraction, 2),
                                  "confidence": 1.0,
                                  "rationale": f"micro profit take; pnl={pnl:.3f}, "
                                               f"sell_fraction={self.micro_profit_sell_fraction:.2f}"})
                continue
            if token in held and excess >= min_rebalance:
                decisions.append({"token": token, "action": "trim", "size_pct": 0.0,
                                  "size_usd": round(excess, 2), "confidence": 1.0,
                                  "rationale": f"rebalance to target; top5={top5_active}, "
                                               f"excess=${excess:.2f}"})
        for s in ranked:                       # enter/keep targets
            if s.token in targets and s.token not in held:
                # hysteresis: skip names we rotated out of within the cooldown (anti-churn)
                if self._now - self._exited_at.get(s.token, -1e18) < self.reentry_cooldown_sec:
                    continue
                needed = max(0.0, target_value - portfolio.get("position_values", {}).get(s.token, 0.0))
                size_pct = needed / cash if cash > 0 else 0.0
                if size_pct <= 0:
                    continue
                conf = min(0.95, 0.55 + abs(s.score) / 2)
                _, entry_kind = self._entry_classification(s, regime, snapshot, quality)
                decisions.append(_dec(s.token, "buy", size_pct, conf,
                                      f"rotate in: {entry_kind}, quality={quality[s.token]:.3f}, "
                                      f"signal={s.score} ({regime.value}), gross={gross:.2f}"))
        return decisions


# --- LLM decider (provider-agnostic: Gemini or Claude) -------------------------
class LLMDecider:
    """Provider-neutral LLM decision layer. Weighs the already-computed signals
    and may override the deterministic decider. Uses whichever LLM key is present
    (see agent.llm); on ANY failure it falls back so the live loop never stalls."""

    def __init__(self, cfg: dict, fallback=None):
        self.cfg = cfg
        self.fallback = fallback or RuleBasedDecider(cfg)

    def decide(self, snapshot, signals, portfolio, risk_limits):
        setattr(self.fallback, "_now", getattr(self, "_now", 0.0))
        candidates = self.fallback.decide(snapshot, signals, portfolio, risk_limits)
        if not self.cfg.get("llm", {}).get("second_gate", False) or not candidates:
            return candidates
        payload = build_snapshot_payload(snapshot, signals, portfolio, risk_limits)
        user = (
            "Current market snapshot:\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            f"Deterministic candidate decisions:\n{json.dumps(candidates)}\n\n"
            "Act only as a risk gate. Approve a candidate by returning the exact same "
            "token and action, or veto it with hold. Never introduce a new token/action. "
            "JSON only."
        )
        text = llm.complete(user, system=SYSTEM_PROMPT,
                            max_tokens=self.cfg["llm"]["max_tokens"])
        if text:
            try:
                s = text[text.find("{"): text.rfind("}") + 1]  # strip any ``` fences
                model = _validate(json.loads(s).get("decisions", []))
                approved = {(d["token"], d["action"]): d for d in model}
                out = []
                for candidate in candidates:
                    # Never let an LLM veto de-risking.
                    if candidate["action"] in ("close", "sell", "trim"):
                        out.append(candidate)
                        continue
                    gate = approved.get((candidate["token"], candidate["action"]))
                    if gate:
                        keep = dict(candidate)
                        keep["confidence"] = min(candidate["confidence"], gate["confidence"])
                        keep["rationale"] += f"; LLM gate: {gate['rationale']}"
                        out.append(keep)
                return out
            except Exception:
                pass  # logged by caller; fall through to deterministic decider
        return candidates


ClaudeDecider = LLMDecider   # backwards-compat alias


def _dec(token, action, size_pct, confidence, rationale):
    return {"token": token, "action": action, "size_pct": round(size_pct, 4),
            "confidence": round(confidence, 4), "rationale": rationale}


def build_decider(cfg: dict):
    policy = cfg.get("decision", {}).get("policy", "threshold")
    base = RotationDecider(cfg) if policy == "rotation" else RuleBasedDecider(cfg)
    # Wrap with the LLM layer if a provider key (Gemini or Anthropic) exists.
    # Active in paper too, so it can be validated before go-live without real money.
    if cfg.get("mode") in ("live", "paper") and llm.available():
        return LLMDecider(cfg, fallback=base)
    return base
