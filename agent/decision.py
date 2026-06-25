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
from dataclasses import dataclass, field

from . import llm
from .signal_engine import Regime, TokenSignal

SYSTEM_PROMPT = """\
You are the review/veto layer of an autonomous trading agent on BNB Smart Chain.
You do NOT choose new trades, execute trades, or compute prices. The deterministic
strategy already produced candidate actions. Your only job is risk review.

RULES:
1. Review ONLY the provided deterministic candidate decisions.
2. NEVER introduce a new token or action.
3. NEVER increase size_pct. You may approve unchanged, reduce size_pct, or veto
   by omitting the candidate / returning hold. Do NOT lower confidence on an
   approved buy: confidence is the deterministic execution gate, not an opinion
   score.
4. NEVER veto de-risking actions: close, sell, trim.
5. For buys, check late chase, falling knife, weak route/liquidity, holder/news risk,
   and whether 1h/24h/7d momentum is already overextended.
6. This is a rank-by-return tournament with a hard drawdown DQ. Avoid needless churn.
7. Always give a short rationale, IN ENGLISH, citing concrete signals.

OUTPUT — EXACTLY this JSON, no markdown:
{"decisions":[{"token":"CAKE","action":"buy|sell|hold|close","size_pct":0.0,
"confidence":0.0,"rationale":"..."}],"portfolio_note":"..."}
"""

_VALID_ACTIONS = {"buy", "sell", "short", "hold", "close", "trim"}


@dataclass(frozen=True)
class TradeIntent:
    """Internal strategy intent before executor/risk-gate validation.

    Keeping the intent shape explicit makes it harder for ranking, sizing, exit,
    and AI-review logic to silently mutate each other. The public contract is
    still the plain decision dict returned by ``as_decision``.
    """

    token: str
    action: str
    size_pct: float = 0.0
    confidence: float = 0.0
    rationale: str = ""
    size_usd: float | None = None

    def as_decision(self) -> dict:
        out = {
            "token": self.token,
            "action": self.action,
            "size_pct": round(max(0.0, min(1.0, float(self.size_pct))), 4),
            "confidence": round(max(0.0, min(1.0, float(self.confidence))), 4),
            "rationale": self.rationale,
        }
        if self.size_usd is not None:
            out["size_usd"] = round(max(0.0, float(self.size_usd)), 2)
        return out


@dataclass
class StrategyState:
    """One tick's deterministic strategy state.

    This is intentionally narrow and serialisable-ish: the agent can later expose
    these fields in debug traces without scraping closure-local variables from
    the monolithic rotation function.
    """

    candidates: list[TokenSignal]
    held: set[str]
    regime: Regime
    quality: dict[str, float] = field(default_factory=dict)
    ranked: list[TokenSignal] = field(default_factory=list)
    validated: set[str] = field(default_factory=set)
    eligible: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    rejects: dict[str, str] = field(default_factory=dict)
    anti_churn: dict[str, str] = field(default_factory=dict)
    gross: float = 0.0
    target_value: float = 0.0
    top5_active: bool = False


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
    # Some symbols can be execution-manageable but intentionally sell-only.
    # Example: a token discovered through portfolio reconciliation may need a
    # contract mapping so the bot can quote/exit it, while new entries remain
    # forbidden until it is deliberately promoted into the buy universe.
    sell_only = set(cfg["twak"].get("sell_only_tokens", []))
    return set(cfg["twak"]["token_contracts"]) - deny - sell_only


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


class CandidateRanker:
    """Builds the executable candidate set and cross-sectional quality ranking."""

    def __init__(self, strategy: "RotationDecider"):
        self.strategy = strategy

    def build_state(self, snapshot: dict, signals: dict[str, TokenSignal],
                    portfolio: dict) -> StrategyState | None:
        buyable = tradeable_buy_tokens(self.strategy.cfg)
        held = {t for t, q in portfolio["positions"].items() if q > 0}
        candidates = [s for s in signals.values() if s.token in buyable or s.token in held]
        if not candidates:
            return None
        regime = candidates[0].regime
        signal_tokens = {s.token for s in candidates}
        held &= signal_tokens
        quality = self.strategy._quality_scores(candidates, snapshot)
        ranked = sorted(candidates, key=lambda s: quality[s.token], reverse=True)
        validated = {s.token for s in candidates
                     if runtime_validated_token(self.strategy.cfg, s.token, snapshot)}
        return StrategyState(candidates=candidates, held=held, regime=regime,
                             quality=quality, ranked=ranked, validated=validated)


class EntryGate:
    """Owns entry confirmation and late-chase/falling-knife filters."""

    def __init__(self, strategy: "RotationDecider"):
        self.strategy = strategy

    def confirmed(self, signal: TokenSignal, threshold: float, held: set[str],
                  risk_limits: dict) -> bool:
        confirmation = self.strategy.cfg.get("decision", {}).get("signal_confirmation", {})
        immediate = float(confirmation.get("immediate_score", 0.40))
        required_ticks = int(confirmation.get("required_ticks", 2))
        streaks = risk_limits.get("signal_streaks", {})
        return (signal.token in held or
                signal.score >= immediate or
                (signal.score > threshold
                 and int(streaks.get(signal.token, 0)) >= required_ticks))

    def allows(self, signal: TokenSignal, state: StrategyState,
               snapshot: dict, risk_limits: dict) -> bool:
        return self.reject_reason(signal, state, snapshot, risk_limits) is None

    def reject_reason(self, signal: TokenSignal, state: StrategyState,
                      snapshot: dict, risk_limits: dict) -> str | None:
        if signal.token in state.held:
            return None
        threshold = (self.strategy.down_min_mom
                     if state.regime is Regime.TREND_DOWN else self.strategy.min_mom)
        if signal.token not in state.validated:
            return "EntryGate:not_runtime_validated"
        if signal.score <= threshold:
            return f"EntryGate:score_below_threshold:{signal.score:.3f}<={threshold:.3f}"
        fresh, _ = self.strategy._entry_classification(signal, state.regime,
                                                       snapshot, state.quality)
        if not fresh:
            _, reason = self.strategy._entry_classification(signal, state.regime,
                                                            snapshot, state.quality)
            return f"EntryGate:{reason}"
        if not self.confirmed(signal, threshold, state.held, risk_limits):
            streak = int(risk_limits.get("signal_streaks", {}).get(signal.token, 0))
            required = int(self.strategy.cfg.get("decision", {})
                           .get("signal_confirmation", {}).get("required_ticks", 2))
            return f"EntryGate:confirmation_streak:{streak}<{required}"
        return None

    def kind(self, signal: TokenSignal, state: StrategyState, snapshot: dict) -> str:
        return self.strategy._entry_classification(signal, state.regime,
                                                   snapshot, state.quality)[1]


class ExitGate:
    """Owns held-token health exits and de-risking reasons."""

    def __init__(self, strategy: "RotationDecider"):
        self.strategy = strategy

    def reason(self, signal: TokenSignal | None, state: StrategyState,
               snapshot: dict, portfolio: dict) -> str | None:
        if signal is None:
            return None
        return self.strategy._held_exit_reason(signal, state.regime, snapshot,
                                               state.quality, portfolio)

    def healthy(self, signal: TokenSignal | None, state: StrategyState,
                snapshot: dict, portfolio: dict) -> bool:
        return self.reason(signal, state, snapshot, portfolio) is None


class SizingPolicy:
    """Owns gross exposure, rebalance, micro-profit and new-entry sizing."""

    def __init__(self, strategy: "RotationDecider"):
        self.strategy = strategy

    def annotate_state(self, state: StrategyState, signals: dict[str, TokenSignal],
                       portfolio: dict, risk_limits: dict, snapshot: dict) -> StrategyState:
        rank = risk_limits.get("leaderboard_rank")
        lb_ret = risk_limits.get("leaderboard_return_pct")
        exec_ret = float(risk_limits.get("executable_return_pct") or 0.0)
        mark_gap = abs(float(lb_ret) - exec_ret) if lb_ret is not None else float("inf")
        state.top5_active = bool(rank and rank <= 5 and exec_ret >= 0
                                 and mark_gap <= self.strategy.max_rank_mark_divergence)
        state.gross = (self.strategy.top5_gross if state.top5_active
                       else self.strategy._catchup_gross(state.targets, signals,
                                                         risk_limits, snapshot,
                                                         state.quality))
        if not state.top5_active:
            state.gross = self.strategy._recovery_escalated_gross(
                state, signals, portfolio, risk_limits, snapshot
            )
        state.target_value = (portfolio["total_equity_usd"] * state.gross
                              / max(len(state.targets), 1))
        return state

    def trim_intent(self, token: str, state: StrategyState, portfolio: dict) -> TradeIntent | None:
        current = portfolio.get("position_values", {}).get(token, 0.0)
        excess = current - state.target_value
        pnl = self.strategy._held_pnl(token, portfolio)
        if (token in state.held and pnl >= self.strategy.micro_profit_take_pct
                and current * self.strategy.micro_profit_sell_fraction
                >= self.strategy.min_micro_profit_sell_usd):
            return TradeIntent(
                token=token, action="trim", size_usd=current * self.strategy.micro_profit_sell_fraction,
                confidence=1.0,
                rationale=(f"micro profit take; pnl={pnl:.3f}, "
                           f"sell_fraction={self.strategy.micro_profit_sell_fraction:.2f}"),
            )
        min_rebalance = float(self.strategy.cfg.get("decision", {}).get("min_rebalance_usd", 1.0))
        if token in state.held and excess >= min_rebalance:
            age = self.strategy._held_age_seconds(token, portfolio)
            if (not state.top5_active
                    and age < self.strategy.min_rebalance_hold_sec):
                return None
            return TradeIntent(
                token=token, action="trim", size_usd=excess, confidence=1.0,
                rationale=f"rebalance to target; top5={state.top5_active}, excess=${excess:.2f}",
            )
        return None

    def buy_intent(self, signal: TokenSignal, state: StrategyState, snapshot: dict,
                   portfolio: dict) -> TradeIntent | None:
        cash = max(portfolio["cash_usd"], 0.0)
        needed = max(0.0, state.target_value
                     - portfolio.get("position_values", {}).get(signal.token, 0.0))
        min_buy_usd = max(
            float(self.strategy.cfg.get("twak", {}).get("min_swap_quote", 0.25)),
            float(self.strategy.cfg.get("decision", {}).get("min_rebalance_usd", 1.0)),
        )
        if needed < min_buy_usd:
            return None
        size_pct = needed / cash if cash > 0 else 0.0
        if size_pct <= 0:
            return None
        conf = min(0.95, 0.55 + abs(signal.score) / 2)
        entry_kind = self.strategy.entry_gate.kind(signal, state, snapshot)
        if signal.token in state.held:
            fresh, reason = self.strategy._entry_classification(
                signal, state.regime, snapshot, state.quality
            )
            if not fresh and not self.strategy._surviving_recovery_target(
                signal.token, state, {signal.token: signal}, portfolio, snapshot
            ):
                state.rejects[signal.token] = f"SizingPolicy:top_up_blocked:{reason}"
                return None
        return TradeIntent(
            token=signal.token,
            action="buy",
            size_pct=size_pct,
            confidence=conf,
            rationale=(f"rotate in: {entry_kind}, quality={state.quality[signal.token]:.3f}, "
                       f"signal={signal.score} ({state.regime.value}), gross={state.gross:.2f}"),
        )


class AntiChurnPolicy:
    """Owns target hysteresis and recently-exited re-entry cooldown."""

    def __init__(self, strategy: "RotationDecider"):
        self.strategy = strategy

    def apply_target_hysteresis(self, state: StrategyState,
                                signals: dict[str, TokenSignal],
                                snapshot: dict, portfolio: dict) -> None:
        state.anti_churn = {}
        for token in state.held:
            if token not in state.eligible or token in state.targets:
                continue
            held_signal = signals.get(token)
            if held_signal is None:
                continue
            if not self.strategy.exit_gate.healthy(held_signal, state, snapshot, portfolio):
                continue
            if len(state.targets) < self.strategy._target_limit(state.regime):
                state.targets.append(token)
                continue
            weakest = min(state.targets, key=lambda t: state.quality[t])
            hurdle = self.strategy._rotation_hurdle_for_held(token, state.regime, portfolio)
            if state.quality[weakest] - state.quality[token] < hurdle:
                state.anti_churn[token] = (
                    f"AntiChurn:kept_held_over_{weakest}:"
                    f"edge={state.quality[weakest] - state.quality[token]:.3f}<hurdle={hurdle:.3f}"
                )
                state.targets[state.targets.index(weakest)] = token

    def can_reenter(self, token: str, portfolio: dict) -> bool:
        persisted_exited_at = portfolio.get("rotation_exited_at", {}) or {}
        last_exit = max(
            float(self.strategy._exited_at.get(token, -1e18) or -1e18),
            float(persisted_exited_at.get(token, -1e18) or -1e18),
        )
        return self.strategy._now - last_exit >= self.strategy.reentry_cooldown_sec

    def reentry_reject_reason(self, token: str, portfolio: dict,
                              state: StrategyState | None = None,
                              signals: dict[str, TokenSignal] | None = None,
                              snapshot: dict | None = None,
                              risk_limits: dict | None = None) -> str | None:
        persisted_exited_at = portfolio.get("rotation_exited_at", {}) or {}
        last_exit = max(
            float(self.strategy._exited_at.get(token, -1e18) or -1e18),
            float(persisted_exited_at.get(token, -1e18) or -1e18),
        )
        remaining = self.strategy.reentry_cooldown_sec - (self.strategy._now - last_exit)
        if remaining > 0:
            if state is not None and signals is not None and snapshot is not None:
                # Tournament recovery mode: don't let a stale anti-churn cooldown
                # suppress a fully validated high-conviction rebound.  The
                # high-conviction predicate already checks catch-up need, x402/CMC
                # confirmation, route friction, token risk, volume, and anti-chase.
                # Ordinary candidates still obey the cooldown.
                if self.strategy._high_conviction_target(
                    token, signals, snapshot, state.quality, risk_limits or {}
                ):
                    state.anti_churn[token] = (
                        f"AntiChurn:reentry_cooldown_bypassed_high_conviction:"
                        f"{remaining:.0f}s_remaining"
                    )
                    return None
            return f"AntiChurn:reentry_cooldown:{remaining:.0f}s_remaining"
        return None


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
        self.scout_exception_enabled = bool(entry_cfg.get(
            "scout_exception_enabled", False))
        self.scout_gross = float(entry_cfg.get("scout_exposure_pct", 0.18))
        self.scout_min_score = float(entry_cfg.get("scout_min_score", 0.31))
        self.scout_min_quality_down = float(entry_cfg.get(
            "scout_min_quality_downtrend", 0.30))
        self.scout_min_r6_down = float(entry_cfg.get(
            "scout_min_return_6h_downtrend", -0.02))
        self.scout_max_r6_down = float(entry_cfg.get(
            "scout_max_return_6h_downtrend", 0.02))
        self.scout_min_r24_down = float(entry_cfg.get(
            "scout_min_return_24h_downtrend", 0.02))
        self.scout_max_c1_down = float(entry_cfg.get(
            "scout_max_cmc_pct_1h_downtrend", self.max_entry_cmc_1h_down))
        self.scout_max_c24_down = float(entry_cfg.get(
            "scout_max_cmc_pct_24h_downtrend", self.max_entry_cmc_24h_down))
        self.scout_max_c7_down = float(entry_cfg.get(
            "scout_max_cmc_pct_7d_downtrend", self.max_entry_cmc_7d_down))
        self.scout_min_x402 = float(entry_cfg.get("scout_min_x402", 0.25))
        self.scout_min_cmc = float(entry_cfg.get("scout_min_cmc", 0.25))
        self.scout_max_round_trip = float(entry_cfg.get(
            "scout_max_round_trip_loss_pct", 1.8))
        self.scout_max_risk = float(entry_cfg.get(
            "scout_max_token_risk_score", 30.0))
        self.scout_min_volume = float(entry_cfg.get(
            "scout_min_volume_24h_usd", 5_000_000))
        self.liquid_continuation_enabled = bool(entry_cfg.get(
            "liquid_continuation_exception_enabled", True))
        self.liquid_continuation_min_r6_down = float(entry_cfg.get(
            "liquid_continuation_min_return_6h_downtrend", 0.02))
        self.liquid_continuation_min_r24_down = float(entry_cfg.get(
            "liquid_continuation_min_return_24h_downtrend", 0.04))
        self.liquid_continuation_min_x402 = float(entry_cfg.get(
            "liquid_continuation_min_x402", 0.25))
        self.liquid_continuation_min_cmc = float(entry_cfg.get(
            "liquid_continuation_min_cmc", 0.20))
        self.liquid_continuation_max_round_trip = float(entry_cfg.get(
            "liquid_continuation_max_round_trip_loss_pct", 1.5))
        self.liquid_continuation_max_risk = float(entry_cfg.get(
            "liquid_continuation_max_token_risk_score", 30.0))
        self.liquid_continuation_min_volume = float(entry_cfg.get(
            "liquid_continuation_min_volume_24h_usd", 10_000_000))
        esc = d.get("recovery_escalation", {}) or {}
        self.recovery_escalation_enabled = bool(esc.get("enabled", False))
        self.recovery_rank_above = int(esc.get("rank_above", 20))
        self.recovery_min_gap_top5 = float(esc.get("min_gap_to_top5_pct", 5.0))
        self.recovery_min_hold_sec = float(esc.get("min_hold_seconds", 600))
        self.recovery_scale_gross = float(esc.get("scale_gross_exposure_pct", 0.36))
        self.recovery_confirmed_hold_sec = float(esc.get("confirmed_hold_seconds", 1800))
        self.recovery_confirmed_gross = float(esc.get("confirmed_gross_exposure_pct", 0.50))
        self.recovery_max_lb_dd = float(esc.get("max_leaderboard_drawdown_pct", 24.0))
        self.recovery_min_score = float(esc.get("min_score", 0.27))
        self.recovery_min_return_6h = float(esc.get("min_return_6h", -0.005))
        self.recovery_min_return_24h = float(esc.get("min_return_24h", 0.02))
        self.recovery_min_x402 = float(esc.get("min_x402", 0.25))
        self.recovery_min_cmc = float(esc.get("min_cmc", -0.05))
        self.recovery_max_round_trip = float(esc.get("max_round_trip_loss_pct", 1.8))
        self.recovery_max_risk = float(esc.get("max_token_risk_score", 30.0))
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
        self.held_exit_floor_buffer_down = float(exit_cfg.get(
            "floor_score_buffer_downtrend", 0.0))
        self.held_exit_floor_buffer_up = float(exit_cfg.get(
            "floor_score_buffer_uptrend", 0.0))
        self.held_min_quality_down = float(exit_cfg.get("min_quality_downtrend", 0.0))
        self.held_min_return_6h_down = float(exit_cfg.get("min_return_6h_downtrend", -0.015))
        self.held_min_hold_sec_down = float(exit_cfg.get("min_hold_seconds_downtrend", 0.0))
        self.held_fresh_hard_floor_down = float(exit_cfg.get(
            "fresh_hard_floor_score_downtrend",
            max(0.0, self.held_exit_floor_down - self.held_exit_floor_buffer_down - 0.08)))
        self.held_stale_loss_pct = float(exit_cfg.get("stale_loss_pct", -0.006))
        self.held_stale_loss_min_r6 = float(exit_cfg.get("stale_loss_min_return_6h", -0.005))
        self.fresh_loss_rotation_min_hold_sec_down = float(exit_cfg.get(
            "fresh_loss_rotation_min_hold_seconds_downtrend", self.held_min_hold_sec_down))
        self.fresh_loss_rotation_hurdle_down = float(exit_cfg.get(
            "fresh_loss_rotation_hurdle_downtrend", max(self.rotation_hurdle, 0.30)))
        self.fresh_loss_rotation_max_pnl = float(exit_cfg.get(
            "fresh_loss_rotation_max_pnl_pct", 0.0))
        self.micro_profit_take_pct = float(exit_cfg.get("micro_profit_take_pct", 0.015))
        self.micro_profit_sell_fraction = float(exit_cfg.get("micro_profit_sell_fraction", 0.45))
        self.min_micro_profit_sell_usd = float(exit_cfg.get("min_micro_profit_sell_usd", 1.0))
        self.min_rebalance_hold_sec = float(d.get(
            "min_rebalance_hold_seconds", self.held_min_hold_sec_down))
        self._now = 0.0                      # set by process_tick each tick (now_ts)
        self._exited_at: dict[str, float] = {}
        self.last_debug: dict = {}
        self.ranker = CandidateRanker(self)
        self.entry_gate = EntryGate(self)
        self.exit_gate = ExitGate(self)
        self.sizing = SizingPolicy(self)
        self.anti_churn = AntiChurnPolicy(self)

    def _target_limit(self, regime: Regime) -> int:
        return self.down_k if regime is Regime.TREND_DOWN else self.k

    def _debug_token(self, signal: TokenSignal | None, state: StrategyState | None,
                     snapshot: dict | None = None) -> dict | None:
        if signal is None:
            return None
        d = (snapshot or {}).get(signal.token, {})
        out = {
            "token": signal.token,
            "score": round(float(signal.score), 4),
            "quality": (round(float(state.quality.get(signal.token)), 4)
                        if state and signal.token in state.quality else None),
            "validated": bool(state and signal.token in state.validated),
            "held": bool(state and signal.token in state.held),
            "target": bool(state and signal.token in state.targets),
        }
        for k in ("return_6h", "return_24h", "vol_adjusted_return",
                  "distance_from_48h_high", "cmc_id", "cmc_rank",
                  "cmc_pct_1h", "cmc_pct_24h", "cmc_pct_7d",
                  "cmc_volume_24h", "cmc_volume_change_24h",
                  "cmc_rsi14", "cmc_macd_state", "cmc_ema_trend",
                  "cmc_top10_holder_pct", "token_news_sentiment",
                  "x402_token_score", "cmc_score", "round_trip_loss_pct",
                  "risk_level", "token_risk_score", "history_bars"):
            if k in d:
                out[k] = d.get(k)
        if state and signal.token in state.rejects:
            out["reject_reason"] = state.rejects[signal.token]
            out["gate"] = str(state.rejects[signal.token]).split(":", 1)[0]
        if state and signal.token in state.anti_churn:
            out["anti_churn"] = state.anti_churn[signal.token]
            out["gate"] = "AntiChurn"
        if not out.get("gate"):
            if out["held"]:
                out["gate"] = "Held"
            elif out["target"]:
                out["gate"] = "Target"
            elif out["validated"]:
                out["gate"] = "Validated"
            else:
                out["gate"] = "RuntimeValidation"
        return out

    def _instrument_coverage(self, state: StrategyState, snapshot: dict) -> dict:
        """Compact per-tick coverage for the tools feeding token decisions."""
        tokens = [s.token for s in state.candidates]

        def has(token: str, *keys: str) -> bool:
            d = snapshot.get(token, {}) or {}
            return any(d.get(k) not in (None, "", {}) for k in keys)

        return {
            "candidates": len(tokens),
            "runtime_validated": len(state.validated),
            "twak_history": sum(1 for t in tokens if has(t, "history_bars", "return_6h")),
            "twak_round_trip": sum(1 for t in tokens if has(t, "round_trip_loss_pct")),
            "x402": sum(1 for t in tokens if has(t, "x402_token_score")),
            "cmc_quote": sum(1 for t in tokens if has(t, "cmc_id", "cmc_pct_24h")),
            "cmc_technicals": sum(1 for t in tokens
                                   if has(t, "cmc_rsi14", "cmc_macd_state", "cmc_ema_trend")),
            "cmc_holder_metrics": sum(1 for t in tokens if has(t, "cmc_top10_holder_pct")),
            "token_news": sum(1 for t in tokens if has(t, "token_news_sentiment")),
        }

    def _candidate_audit(self, state: StrategyState, decisions: list[dict],
                         snapshot: dict) -> list[dict]:
        """Per-token verdicts for operator/debug visibility.

        Trading decisions are still made by EntryGate/ExitGate/Sizing/AntiChurn.
        This audit simply records why strong names did or did not survive those
        layers, so the operator can see whether a token was stopped by TWAK
        validation, CMC/late-chase filters, anti-churn, sizing, or AI review.
        """
        limit = int(self.cfg.get("dashboard", {}).get(
            "candidate_audit_limit",
            self.cfg.get("decision", {}).get("candidate_audit_limit", 30),
        ))
        if limit <= 0:
            return []
        by_token = {s.token: s for s in state.ranked}
        selected: list[TokenSignal] = []

        def add(sig: TokenSignal | None) -> None:
            if sig and all(existing.token != sig.token for existing in selected):
                selected.append(sig)

        for s in state.ranked[:limit]:
            add(s)
        for s in sorted(state.ranked, key=lambda sig: sig.score, reverse=True)[:max(8, limit // 2)]:
            add(s)
        for token in set(state.held) | set(state.targets) | set(state.eligible):
            add(by_token.get(token))
        for d in decisions:
            add(by_token.get(d.get("token")))

        rows = [self._debug_token(s, state, snapshot) for s in selected if s]
        return [r for r in rows if r][:limit]

    def _set_debug(self, state: StrategyState | None, decisions: list[dict],
                   snapshot: dict, suppression_source: str | None = None) -> None:
        if state is None:
            self.last_debug = {
                "layer": "RotationDecider",
                "suppression_source": suppression_source or "CandidateRanker:no_candidates",
                "candidate_decisions": decisions,
            }
            return
        priority = set(state.held) | set(state.targets)
        top_signals = state.ranked[:8]
        for s in state.ranked:
            if s.token in priority and all(existing.token != s.token for existing in top_signals):
                top_signals.append(s)
        top = [self._debug_token(s, state, snapshot) for s in top_signals]
        debug_tokens = {s.token for s in state.ranked[:20]} | set(state.held) | set(state.targets)
        self.last_debug = {
            "layer": "RotationDecider",
            "regime": state.regime.value,
            "held": sorted(state.held),
            "validated": sorted(state.validated),
            "eligible": state.eligible,
            "targets": state.targets,
            "gross": round(float(state.gross), 4),
            "target_value": round(float(state.target_value), 4),
            "top5_active": state.top5_active,
            "top_ranked": top,
            "candidate_audit": self._candidate_audit(state, decisions, snapshot),
            "instrument_coverage": self._instrument_coverage(state, snapshot),
            "rejects": {k: v for k, v in state.rejects.items() if k in debug_tokens},
            "anti_churn": dict(state.anti_churn),
            "candidate_decisions": decisions,
            "suppression_source": suppression_source,
        }

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

    @staticmethod
    def _safe_float(value, default: float | None = None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _gap_to_top5_pct(self, risk_limits: dict) -> float | None:
        top5 = self._safe_float(risk_limits.get("leaderboard_top5_return_pct"))
        own = self._safe_float(
            risk_limits.get("leaderboard_return_pct"),
            self._safe_float(risk_limits.get("executable_return_pct"), 0.0),
        )
        if top5 is None or own is None:
            return None
        return top5 - own

    def _recovery_escalated_gross(self, state: StrategyState,
                                  signals: dict[str, TokenSignal],
                                  portfolio: dict, risk_limits: dict,
                                  snapshot: dict) -> float:
        """Scale surviving scouts while we are far behind, without changing entry rules.

        A scout's first job is discovery.  If it survives the minimum hold and
        remains route-cheap/validated, staying at toe-hold size leaves us unable
        to close a large leaderboard gap.  This ladder only tops up existing
        targets; it never turns a brand-new candidate into an oversized entry.
        """
        if not self.recovery_escalation_enabled or not state.targets:
            return state.gross
        try:
            rank = int(risk_limits.get("leaderboard_rank"))
        except (TypeError, ValueError):
            rank = 999
        gap = self._gap_to_top5_pct(risk_limits)
        if rank <= self.recovery_rank_above or gap is None or gap < self.recovery_min_gap_top5:
            return state.gross
        lb_dd = self._safe_float(risk_limits.get("leaderboard_drawdown_pct"), 0.0) or 0.0
        if lb_dd >= self.recovery_max_lb_dd:
            return state.gross
        if not set(state.targets).issubset(state.held):
            return state.gross

        ages = [self._held_age_seconds(t, portfolio) for t in state.targets]
        if not ages or min(ages) < self.recovery_min_hold_sec:
            return state.gross
        if not all(self._surviving_recovery_target(t, state, signals, portfolio, snapshot)
                   for t in state.targets):
            return state.gross

        target = self.recovery_scale_gross
        if min(ages) >= self.recovery_confirmed_hold_sec:
            target = self.recovery_confirmed_gross
        return min(self.target_gross, max(state.gross, target))

    def _surviving_recovery_target(self, token: str, state: StrategyState,
                                   signals: dict[str, TokenSignal],
                                   portfolio: dict, snapshot: dict) -> bool:
        """A held scout can be topped up if it is still healthy, not freshly perfect.

        New entries must clear the stricter scout/entry gate.  Existing recovery
        holdings should not lose their scale-up path merely because the score
        dipped a few bps while the route, confirmation source, and 24h structure
        remain intact.
        """
        s = signals.get(token)
        if s is None or token not in state.validated:
            return False
        if self.exit_gate.reason(s, state, snapshot, portfolio):
            return False
        d = snapshot.get(token, {})
        x402 = float(d.get("x402_token_score", 0.0) or 0.0)
        cmc = float(d.get("cmc_score", 0.0) or 0.0)
        quality = float(state.quality.get(token, -1.0))
        c1 = float(d.get("cmc_pct_1h", 0.0) or 0.0)
        c24 = float(d.get("cmc_pct_24h", 0.0) or 0.0)
        c7 = float(d.get("cmc_pct_7d", 0.0) or 0.0)
        if (
            c1 > self.max_entry_cmc_1h_down
            or c24 > self.max_entry_cmc_24h_down
            or c7 > self.max_entry_cmc_7d_down
        ):
            return False
        # x402 coverage is sparse.  If a position was admitted as a tiny
        # CMC/TWAK-confirmed scout, let it scale only after surviving the minimum
        # hold and still showing very high CMC + cross-sectional quality.  This
        # keeps the x402-backed path intact while preventing CMC-weak names from
        # using the broader no-x402 recovery lane.
        confirmation_ok = (
            x402 >= self.recovery_min_x402
            or (cmc >= max(self.recovery_min_cmc, 0.75)
                and quality >= max(self.scout_min_quality_down, 0.65))
        )
        max_route = max(self.recovery_max_round_trip, self.scout_max_round_trip)
        return (
            float(s.score) >= self.recovery_min_score
            and float(d.get("return_6h", 0.0) or 0.0) >= self.recovery_min_return_6h
            and float(d.get("return_24h", 0.0) or 0.0) >= self.recovery_min_return_24h
            and confirmation_ok
            and cmc >= self.recovery_min_cmc
            and float(d.get("round_trip_loss_pct", 100.0) or 100.0) <= max_route
            and float(d.get("token_risk_score", 100.0) or 100.0) <= self.recovery_max_risk
        )

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
            and not self._scout_exception(s, snapshot, quality)
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

    def _scout_exception(self, signal: TokenSignal, snapshot: dict,
                         quality: dict[str, float]) -> bool:
        """Allow a small recovery probe when x402/CMC/route agree before 6h turns green.

        This is deliberately smaller and stricter than a normal entry.  It exists
        for tournament catch-up mode: a validated token with low route friction,
        positive 24h structure, and non-terrible 6h can get a toe-hold instead of
        forcing the bot to stay 100% cash until the move is already obvious.
        """
        if not self.scout_exception_enabled or signal.regime is not Regime.TREND_DOWN:
            return False
        d = snapshot.get(signal.token, {})
        r6 = float(d.get("return_6h", 0.0) or 0.0)
        r24 = float(d.get("return_24h", 0.0) or 0.0)
        c1 = float(d.get("cmc_pct_1h", 0.0) or 0.0)
        c24 = float(d.get("cmc_pct_24h", 0.0) or 0.0)
        c7 = float(d.get("cmc_pct_7d", 0.0) or 0.0)
        return (
            float(signal.score) >= self.scout_min_score
            and float(quality.get(signal.token, -1.0)) >= self.scout_min_quality_down
            and self.scout_min_r6_down <= r6 <= self.scout_max_r6_down
            and r24 >= self.scout_min_r24_down
            and c1 <= self.scout_max_c1_down
            and c24 <= self.scout_max_c24_down
            and c7 <= self.scout_max_c7_down
            and float(d.get("x402_token_score", 0.0) or 0.0) >= self.scout_min_x402
            and float(d.get("cmc_score", 0.0) or 0.0) >= self.scout_min_cmc
            and float(d.get("round_trip_loss_pct", 100.0) or 100.0) <= self.scout_max_round_trip
            and float(d.get("token_risk_score", 100.0) or 100.0) <= self.scout_max_risk
            and float(d.get("cmc_volume_24h", 0.0) or 0.0) >= self.scout_min_volume
        )

    def _liquid_confirmed_continuation(self, signal: TokenSignal, snapshot: dict,
                                       quality: dict[str, float]) -> bool:
        """Bypass weak volume-change noise for liquid, confirmed comeback trends.

        CMC volume-change can be negative after the first impulse even when
        absolute volume, x402, CMC momentum, route friction, and risk all still
        confirm an executable continuation.  In catch-up mode we should not let
        that single derivative metric suppress a small/medium recovery entry.
        Late-hot and near-high guards still run before this exception.
        """
        if not self.liquid_continuation_enabled or signal.regime is not Regime.TREND_DOWN:
            return False
        d = snapshot.get(signal.token, {})
        r6 = float(d.get("return_6h", 0.0) or 0.0)
        r24 = float(d.get("return_24h", 0.0) or 0.0)
        return (
            float(signal.score) >= self.down_min_mom
            and float(quality.get(signal.token, -1.0)) >= self.min_entry_quality_down
            and self.liquid_continuation_min_r6_down <= r6 <= self.max_entry_r6_down
            and r24 >= self.liquid_continuation_min_r24_down
            and float(d.get("cmc_pct_1h", 0.0) or 0.0) <= self.max_entry_cmc_1h_down
            and float(d.get("cmc_pct_24h", 0.0) or 0.0) <= self.max_entry_cmc_24h_down
            and float(d.get("cmc_pct_7d", 0.0) or 0.0) <= self.max_entry_cmc_7d_down
            and float(d.get("x402_token_score", 0.0) or 0.0) >= self.liquid_continuation_min_x402
            and float(d.get("cmc_score", 0.0) or 0.0) >= self.liquid_continuation_min_cmc
            and float(d.get("round_trip_loss_pct", 100.0) or 100.0) <= self.liquid_continuation_max_round_trip
            and float(d.get("token_risk_score", 100.0) or 100.0) <= self.liquid_continuation_max_risk
            and float(d.get("cmc_volume_24h", 0.0) or 0.0) >= self.liquid_continuation_min_volume
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
        scout_targets = [t for t in targets
                         if t in signals and self._scout_exception(signals[t], snapshot, quality)]
        high_conviction_targets = [t for t in targets
                                   if self._high_conviction_target(t, signals, snapshot,
                                                                   quality, risk_limits)]
        if high_conviction_targets:
            gross = max(gross, self.ds_hc_gross)
        elif pullback_targets:
            gross = max(gross, self.pullback_gross)
        elif scout_targets:
            gross = min(gross, self.scout_gross)

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
            if self._scout_exception(signal, snapshot, quality):
                return True, "validated_scout"
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
            if self._liquid_confirmed_continuation(signal, snapshot, quality):
                return True, "liquid_confirmed_continuation"
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

    def _held_age_seconds(self, token: str, portfolio: dict) -> float:
        opened_ts = float(portfolio.get("position_opened_ts", {}).get(token, 0.0) or 0.0)
        return self._now - opened_ts if opened_ts > 0 and self._now > 0 else float("inf")

    def _rotation_hurdle_for_held(self, token: str, regime: Regime, portfolio: dict) -> float:
        """Use a bigger challenger edge before churning a fresh loser.

        Recovery/scout entries often sit a few bps under water for one or two
        5-minute ticks because of route friction.  Rotating them immediately
        converts noise into realized spread loss, then the strategy can re-enter
        the same theme higher.  Hard stops and explicit health exits still run
        elsewhere; this only raises the opportunity-cost hurdle for discretionary
        rotation.
        """
        if regime is not Regime.TREND_DOWN:
            return self.rotation_hurdle
        age = self._held_age_seconds(token, portfolio)
        pnl = self._held_pnl(token, portfolio)
        if (age < self.fresh_loss_rotation_min_hold_sec_down
                and pnl <= self.fresh_loss_rotation_max_pnl):
            return max(self.rotation_hurdle, self.fresh_loss_rotation_hurdle_down)
        return self.rotation_hurdle

    def _held_exit_reason(self, signal: TokenSignal, regime: Regime, snapshot: dict,
                          quality: dict[str, float], portfolio: dict) -> str | None:
        if not self.held_exit_enabled:
            return None
        token = signal.token
        floor = self._held_floor(regime)
        floor_buffer = (self.held_exit_floor_buffer_down
                        if regime is Regime.TREND_DOWN
                        else self.held_exit_floor_buffer_up)
        effective_floor = floor - max(0.0, floor_buffer)
        age_sec = self._held_age_seconds(token, portfolio)
        if signal.score < effective_floor:
            if (regime is Regime.TREND_DOWN
                    and age_sec < self.held_min_hold_sec_down
                    and signal.score >= self.held_fresh_hard_floor_down):
                return None
            return (f"health exit: score {signal.score:.3f} < "
                    f"floor {effective_floor:.3f} (base {floor:.3f})")
        if regime is Regime.TREND_DOWN:
            d = snapshot.get(token, {})
            q = quality.get(token, 0.0)
            r6 = float(d.get("return_6h", 0.0) or 0.0)
            pnl = self._held_pnl(token, portfolio)
            if q < self.held_min_quality_down and age_sec >= self.held_min_hold_sec_down:
                return f"health exit: quality {q:.3f} < {self.held_min_quality_down:.3f}"
            if r6 < self.held_min_return_6h_down and age_sec >= self.held_min_hold_sec_down:
                return f"health exit: 6h momentum {r6:.3f} < {self.held_min_return_6h_down:.3f}"
            if (pnl <= self.held_stale_loss_pct and r6 < self.held_stale_loss_min_r6
                    and age_sec >= self.held_min_hold_sec_down):
                return f"health exit: stale loss pnl={pnl:.3f}, r6={r6:.3f}"
        return None

    def decide(self, snapshot, signals: dict[str, TokenSignal], portfolio, risk_limits):
        state = self.ranker.build_state(snapshot, signals, portfolio)
        if state is None:
            self._set_debug(None, [], snapshot, "CandidateRanker:no_candidates")
            return []

        if state.regime is Regime.CHOP:
            self._set_debug(state, [], snapshot, "RegimeGate:chop")
            return []                          # hold current; stops still run upstream

        def eligible_target(s):
            if s.token in state.held:
                # Held-token health rule: do not keep a decayed position just
                # because no new token is compelling.  This is strategy, not a
                # manual call: score below the configured floor exits to cash.
                reason = self.exit_gate.reason(s, state, snapshot, portfolio)
                if reason:
                    state.rejects[s.token] = f"ExitGate:{reason}"
                    return False
                return True
            # New entries/rotate-ins require durable execution validation before
            # they can become targets.  High CMC momentum alone cannot bypass it.
            reason = self.entry_gate.reject_reason(s, state, snapshot, risk_limits)
            if reason:
                state.rejects[s.token] = reason
                return False
            return True

        state.eligible = [s.token for s in state.ranked if eligible_target(s)]
        state.targets = state.eligible[:self._target_limit(state.regime)]

        # Hysteresis based on opportunity cost: keep a valid held name unless a
        # challenger is materially better after spread/risk penalties.
        self.anti_churn.apply_target_hysteresis(state, signals, snapshot, portfolio)

        decisions = []
        for t in state.held:                   # exit names no longer targeted
            if t not in state.targets:
                self._exited_at[t] = self._now   # arm the re-entry cooldown
                held_signal = signals.get(t)
                reason = self.exit_gate.reason(held_signal, state, snapshot, portfolio)
                decisions.append(TradeIntent(
                    token=t,
                    action="close",
                    confidence=0.78 if reason else 0.7,
                    rationale=reason or f"rotate out ({state.regime.value})",
                ).as_decision())

        self.sizing.annotate_state(state, signals, portfolio, risk_limits, snapshot)
        for token in state.targets:
            intent = self.sizing.trim_intent(token, state, portfolio)
            if intent:
                decisions.append(intent.as_decision())

        for s in state.ranked:                 # enter/top-up/keep targets
            if s.token not in state.targets:
                continue
            if s.token not in state.held:
                # hysteresis: skip names we rotated out of within the cooldown (anti-churn)
                reentry_reason = self.anti_churn.reentry_reject_reason(
                    s.token, portfolio, state, signals, snapshot, risk_limits
                )
                if reentry_reason:
                    state.rejects[s.token] = reentry_reason
                    continue
            intent = self.sizing.buy_intent(s, state, snapshot, portfolio)
            if intent:
                decisions.append(intent.as_decision())
        suppression = None
        if not decisions:
            if state.targets and state.held and set(state.targets).issubset(state.held):
                suppression = "TargetGate:already_holding_target"
            elif state.targets:
                blocked = [state.rejects.get(t) for t in state.targets if t in state.rejects]
                suppression = blocked[0] if blocked else "SizingPolicy:no_cash_or_target_already_met"
            elif state.rejects:
                first = state.ranked[0].token if state.ranked else None
                suppression = state.rejects.get(first) or next(iter(state.rejects.values()))
            else:
                suppression = "TargetGate:no_eligible_targets"
        self._set_debug(state, decisions, snapshot, suppression)
        return decisions


# --- LLM decider (provider-agnostic: Gemini or Claude) -------------------------
class LLMDecider:
    """Provider-neutral AI review layer.

    Gemini/Claude can veto or shrink deterministic buy candidates after reviewing
    late-chase/falling-knife/news/liquidity risk. It cannot invent actions, cannot
    increase size, and cannot block close/sell/trim exits. On ANY failure it falls
    back so the live loop never stalls.
    """

    def __init__(self, cfg: dict, fallback=None):
        self.cfg = cfg
        self.fallback = fallback or RuleBasedDecider(cfg)
        self.last_debug: dict = {}

    def _cash_veto_override(self, candidate: dict, base_debug: dict,
                            risk_limits: dict, vetoed: list[dict]) -> dict | None:
        """Allow comeback mode to override generic AI cash-preservation vetoes.

        The model is useful for hard vetoes (route/liquidity/scam/news risk), but
        it can become too conservative in a rank-by-return tournament and reject
        every buy simply because the market is in trend_down.  If the
        deterministic stack found a validated, low-risk, high-conviction setup,
        keep a reduced recovery-size entry instead of sitting fully in cash.
        """
        llm_cfg = self.cfg.get("llm", {}) or {}
        if not bool(llm_cfg.get("cash_veto_override_enabled", True)):
            return None
        if candidate.get("action") != "buy":
            return None
        token = candidate.get("token")
        veto_text = " ".join(
            str(v.get("rationale", ""))
            for v in vetoed
            if v.get("token") == token
        ).lower()
        hard_risk_words = (
            "no route", "route", "liquidity", "honeypot", "scam", "exploit",
            "hack", "holder", "whale", "concentration", "news risk", "delist",
        )
        if any(word in veto_text for word in hard_risk_words):
            return None
        rank = risk_limits.get("leaderboard_rank")
        if rank is not None and int(rank) <= int(llm_cfg.get("cash_veto_override_rank_floor", 5)):
            return None
        lb_dd = risk_limits.get("leaderboard_drawdown_pct")
        max_dd = float(llm_cfg.get("cash_veto_override_max_leaderboard_dd_pct", 24.0))
        if lb_dd is not None and float(lb_dd) >= max_dd:
            return None

        details = {}
        for row in (base_debug.get("top_ranked") or []):
            if row.get("token") == token:
                details = row
                break
        if not details:
            return None
        score = float(details.get("score") or 0.0)
        cmc = float(details.get("cmc_score") or 0.0)
        x402 = float(details.get("x402_token_score") or 0.0)
        round_trip = float(details.get("round_trip_loss_pct") or 999.0)
        token_risk = float(details.get("token_risk_score") or 999.0)
        r6 = float(details.get("return_6h") or 0.0)
        entry_cfg = self.cfg.get("decision", {}).get("entry_filter", {}) or {}
        is_scout = "validated_scout" in str(candidate.get("rationale", ""))
        min_score = float(llm_cfg.get(
            "cash_veto_override_scout_min_score" if is_scout else "cash_veto_override_min_score",
            entry_cfg.get("scout_min_score", 0.31) if is_scout else 0.35))
        min_cmc = float(llm_cfg.get(
            "cash_veto_override_scout_min_cmc" if is_scout else "cash_veto_override_min_cmc",
            entry_cfg.get("scout_min_cmc", 0.25) if is_scout else 0.75))
        min_x402 = float(llm_cfg.get(
            "cash_veto_override_scout_min_x402" if is_scout else "cash_veto_override_min_x402",
            entry_cfg.get("scout_min_x402", 0.25) if is_scout else 0.25))
        max_round_trip = float(llm_cfg.get(
            "cash_veto_override_scout_max_round_trip_loss_pct"
            if is_scout else "cash_veto_override_max_round_trip_loss_pct",
            entry_cfg.get("scout_max_round_trip_loss_pct", 1.8) if is_scout else 2.5))
        max_risk = float(llm_cfg.get(
            "cash_veto_override_scout_max_token_risk_score"
            if is_scout else "cash_veto_override_max_token_risk_score",
            entry_cfg.get("scout_max_token_risk_score", 30.0) if is_scout else 30.0))
        min_r6 = float(llm_cfg.get(
            "cash_veto_override_scout_min_return_6h"
            if is_scout else "cash_veto_override_min_return_6h",
            entry_cfg.get("scout_min_return_6h_downtrend", -0.02) if is_scout else -0.005))
        if score < min_score:
            return None
        if cmc < min_cmc:
            return None
        if x402 < min_x402:
            return None
        if round_trip > max_round_trip:
            return None
        if token_risk > max_risk:
            return None
        if r6 < min_r6:
            return None

        keep = dict(candidate)
        max_size = float(llm_cfg.get(
            "cash_veto_override_scout_size_pct" if is_scout else "cash_veto_override_size_pct",
            entry_cfg.get("scout_exposure_pct", 0.18) if is_scout else 0.40))
        keep["size_pct"] = min(float(candidate.get("size_pct", 0.0)), max_size)
        keep["confidence"] = candidate["confidence"]
        kind = "scout" if is_scout else "high-conviction"
        keep["rationale"] += f"; AI cash-veto overridden by {kind} recovery guardrail"
        return keep

    def decide(self, snapshot, signals, portfolio, risk_limits):
        setattr(self.fallback, "_now", getattr(self, "_now", 0.0))
        candidates = self.fallback.decide(snapshot, signals, portfolio, risk_limits)
        base_debug = dict(getattr(self.fallback, "last_debug", {}) or {})
        self.last_debug = {**base_debug, "ai_review": {"active": False}}
        if not self.cfg.get("llm", {}).get("second_gate", False) or not candidates:
            return candidates
        exits = [d for d in candidates if d.get("action") in ("close", "sell", "trim")]
        reviewable = [d for d in candidates if d.get("action") not in ("close", "sell", "trim")][:5]
        if not reviewable:
            self.last_debug = {**base_debug, "ai_review": {
                "active": True,
                "reviewable": [],
                "passed_through": "de-risking_only",
            }}
            return candidates
        payload = build_snapshot_payload(snapshot, signals, portfolio, risk_limits)
        focused_tokens = {d["token"] for d in reviewable}
        focused_payload = {
            **payload,
            "tokens": {t: v for t, v in payload["tokens"].items() if t in focused_tokens},
        }
        user = (
            "Focused market snapshot for deterministic top candidates:\n"
            f"{json.dumps(focused_payload, ensure_ascii=False)}\n\n"
            f"Reviewable deterministic candidates, max 5:\n{json.dumps(reviewable)}\n\n"
            f"De-risking candidates that MUST pass through unchanged:\n{json.dumps(exits)}\n\n"
            "Act only as a veto/review gate. Approve a buy by returning the exact same "
            "token and action with size_pct <= candidate size_pct. Do not reduce confidence "
            "for an approved buy; use size_pct reduction or veto instead. Veto by omitting "
            "the candidate or returning hold. Never introduce a new token/action. JSON only."
        )
        text = llm.complete(user, system=SYSTEM_PROMPT,
                            max_tokens=self.cfg["llm"]["max_tokens"])
        if text:
            try:
                s = text[text.find("{"): text.rfind("}") + 1]  # strip any ``` fences
                model = _validate(json.loads(s).get("decisions", []))
                approved = {(d["token"], d["action"]): d for d in model}
                explicit_holds = {d["token"]: d for d in model if d.get("action") == "hold"}
                out = []
                approved_tokens = []
                reduced = []
                vetoed = []
                for candidate in candidates:
                    # Never let an LLM veto de-risking.
                    if candidate["action"] in ("close", "sell", "trim"):
                        out.append(candidate)
                        continue
                    gate = approved.get((candidate["token"], candidate["action"]))
                    if gate:
                        keep = dict(candidate)
                        keep["size_pct"] = min(float(candidate.get("size_pct", 0.0)),
                                               float(gate.get("size_pct", 0.0)))
                        # Gemini/Claude is a veto + size-review layer.  Do not let
                        # the model silently turn an approved buy into a
                        # risk_gate low_confidence block; if it dislikes the
                        # trade it must veto or reduce size.
                        keep["confidence"] = candidate["confidence"]
                        if keep["size_pct"] > 0:
                            keep["rationale"] += f"; AI review: {gate['rationale']}"
                            out.append(keep)
                            approved_tokens.append(candidate["token"])
                            if keep["size_pct"] < float(candidate.get("size_pct", 0.0)):
                                reduced.append({
                                    "token": candidate["token"],
                                    "from_size_pct": candidate.get("size_pct"),
                                    "to_size_pct": keep["size_pct"],
                                    "from_confidence": candidate.get("confidence"),
                                    "to_confidence": keep["confidence"],
                                    "rationale": gate.get("rationale", ""),
                                })
                        else:
                            # Explicit size=0 is treated as a veto.
                            vetoed.append({
                                "token": candidate["token"],
                                "action": candidate["action"],
                                "rationale": gate.get("rationale", "size_pct=0"),
                            })
                            continue
                    elif candidate["action"] not in ("close", "sell", "trim"):
                        hold = explicit_holds.get(candidate["token"], {})
                        vetoed.append({
                            "token": candidate["token"],
                            "action": candidate["action"],
                            "rationale": hold.get("rationale", "omitted_by_ai_review"),
                        })
                source = base_debug.get("suppression_source")
                override = None
                if not any(d.get("action") == "buy" for d in out) and vetoed:
                    for candidate in reviewable:
                        override = self._cash_veto_override(candidate, base_debug,
                                                            risk_limits, vetoed)
                        if override:
                            out.append(override)
                            reduced.append({
                                "token": override["token"],
                                "from_size_pct": candidate.get("size_pct"),
                                "to_size_pct": override["size_pct"],
                                "from_confidence": candidate.get("confidence"),
                                "to_confidence": override["confidence"],
                                "rationale": "cash_veto_override",
                            })
                            source = "GeminiReview:cash_veto_overridden"
                            break
                if not out and vetoed:
                    source = "GeminiReview:vetoed_all_buys"
                elif vetoed and not [d for d in out if d.get("action") == "buy"]:
                    source = "GeminiReview:vetoed_buys"
                self.last_debug = {**base_debug, "ai_review": {
                    "active": True,
                    "provider": llm.provider(),
                    "reviewable": reviewable,
                    "model_decisions": model,
                    "approved": approved_tokens,
                    "reduced": reduced,
                    "vetoed": vetoed,
                }, "pre_ai_candidates": candidates, "post_ai_candidates": out,
                   "suppression_source": source}
                return out
            except Exception:
                self.last_debug = {**base_debug, "ai_review": {
                    "active": True,
                    "provider": llm.provider(),
                    "reviewable": reviewable,
                    "error": "parse_or_validation_failed",
                }}
                pass  # logged by caller; fall through to deterministic decider
        else:
            self.last_debug = {**base_debug, "ai_review": {
                "active": True,
                "provider": llm.provider(),
                "reviewable": reviewable,
                "error": "no_model_text_fallback",
            }}
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
