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


def executable_validated_token(cfg: dict, token: str) -> bool:
    """True when a token has passed the dynamic executable round-trip probe.

    `deny_buy` is intentionally not always permanent. Some names (notably ZETA)
    were quarantined because a prior sell/route failed, not because the symbol is
    intrinsically malicious. A quarantined token can be re-enabled only after the
    UniverseManager records a successful buy->sell validation with acceptable
    round-trip loss.
    """
    meta = (cfg.get("universe_runtime") or {}).get(token) or {}
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
        self._now = 0.0                      # set by process_tick each tick (now_ts)
        self._exited_at: dict[str, float] = {}

    def decide(self, snapshot, signals: dict[str, TokenSignal], portfolio, risk_limits):
        tradeable = tradeable_buy_tokens(self.cfg)
        cand = [s for s in signals.values() if s.token in tradeable]
        if not cand:
            return []
        regime = cand[0].regime
        held = {t for t, q in portfolio["positions"].items() if q > 0 and t in tradeable}

        if regime is Regime.CHOP:
            return []                          # hold current; stops still run upstream

        def relative(field: str) -> dict[str, float]:
            vals = {s.token: float(snapshot.get(s.token, {}).get(field, 0.0)) for s in cand}
            if not vals or max(vals.values()) - min(vals.values()) < 1e-12:
                return {s.token: 0.0 for s in cand}
            ordered = sorted(cand, key=lambda s: vals[s.token])
            if len(ordered) <= 1:
                return {s.token: 0.0 for s in ordered}
            return {s.token: 2.0 * i / (len(ordered) - 1) - 1.0
                    for i, s in enumerate(ordered)}

        rel6, rel24 = relative("return_6h"), relative("return_24h")
        quality = {}
        for s in cand:
            d = snapshot.get(s.token, {})
            vol_adj = max(-1.0, min(1.0, float(d.get("vol_adjusted_return", 0.0)) / 3.0))
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
                                + 0.18 * cmc
                                - 0.75 * spread - 0.10 * risk - chase_penalty
                                - liquidity_penalty - rank_penalty - holder_penalty
                                - ambiguous_penalty)
        ranked = sorted(cand, key=lambda s: quality[s.token], reverse=True)
        confirmation = self.cfg.get("decision", {}).get("signal_confirmation", {})
        immediate = float(confirmation.get("immediate_score", 0.40))
        required_ticks = int(confirmation.get("required_ticks", 2))
        streaks = risk_limits.get("signal_streaks", {})

        def confirmed(s, threshold):
            return (s.token in held or
                    (s.score >= immediate) or
                    (s.score > threshold and int(streaks.get(s.token, 0)) >= required_ticks))

        if regime is Regime.TREND_DOWN:         # only the strongest counter-trend names
            eligible = [s.token for s in ranked if s.score > self.down_min_mom
                        and confirmed(s, self.down_min_mom)]
            limit = self.down_k
        else:
            eligible = [s.token for s in ranked if s.score > self.min_mom
                        and confirmed(s, self.min_mom)]
            limit = self.k
        targets = eligible[:limit]

        # Hysteresis based on opportunity cost: keep a valid held name unless a
        # challenger is materially better after spread/risk penalties.
        for token in held:
            if token not in eligible or token in targets:
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
                decisions.append(_dec(t, "close", 0.0, 0.7, f"rotate out ({regime.value})"))
        rank = risk_limits.get("leaderboard_rank")
        lb_ret = risk_limits.get("leaderboard_return_pct")
        exec_ret = float(risk_limits.get("executable_return_pct") or 0.0)
        mark_gap = abs(float(lb_ret) - exec_ret) if lb_ret is not None else float("inf")
        top5_active = bool(rank and rank <= 5 and exec_ret >= 0
                           and mark_gap <= self.max_rank_mark_divergence)
        gross = self.top5_gross if top5_active else self.target_gross
        target_value = portfolio["total_equity_usd"] * gross / max(len(targets), 1)
        cash = max(portfolio["cash_usd"], 0.0)
        min_rebalance = float(self.cfg.get("decision", {}).get("min_rebalance_usd", 1.0))
        for token in targets:
            current = portfolio.get("position_values", {}).get(token, 0.0)
            excess = current - target_value
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
                decisions.append(_dec(s.token, "buy", size_pct, conf,
                                      f"rotate in: quality={quality[s.token]:.3f}, "
                                      f"signal={s.score} ({regime.value})"))
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
