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

_VALID_ACTIONS = {"buy", "sell", "short", "hold", "close"}


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
                    "btc_dominance", "news_sentiment")},
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

    def decide(self, snapshot, signals: dict[str, TokenSignal], portfolio, risk_limits):
        cand = [s for s in signals.values() if s.token in self.tradeable]
        if not cand:
            return []
        regime = cand[0].regime
        held = {t for t, q in portfolio["positions"].items() if q > 0 and t in self.tradeable}

        if regime is Regime.CHOP:
            return []                          # hold current; stops still run upstream

        ranked = sorted(cand, key=lambda s: s.score, reverse=True)
        if regime is Regime.TREND_DOWN:         # only the strongest counter-trend names
            targets = [s.token for s in ranked if s.score > self.down_min_mom][: self.down_k]
        else:
            targets = [s.token for s in ranked if s.score > self.min_mom][: self.k]

        decisions = []
        for t in held:                         # exit names no longer targeted
            if t not in targets:
                decisions.append(_dec(t, "close", 0.0, 0.7, f"rotate out ({regime.value})"))
        per_name = min(self.cfg["risk"]["max_position_pct"], 1.0 / max(self.k, 1))
        for s in ranked:                       # enter/keep targets
            if s.token in targets and s.token not in held:
                conf = min(0.95, 0.55 + abs(s.score) / 2)
                decisions.append(_dec(s.token, "buy", per_name, conf,
                                      f"rotate in: rank momentum={s.score} ({regime.value})"))
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
        payload = build_snapshot_payload(snapshot, signals, portfolio, risk_limits)
        user = (
            "Current market snapshot:\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return decisions in the given JSON schema. JSON only."
        )
        text = llm.complete(user, system=SYSTEM_PROMPT,
                            max_tokens=self.cfg["llm"]["max_tokens"])
        if text:
            try:
                s = text[text.find("{"): text.rfind("}") + 1]  # strip any ``` fences
                decisions = _validate(json.loads(s).get("decisions", []))
                # Spot-only safety net: if the model ignores the prompt and proposes
                # a short while perps are disabled, drop it (never reaches executor).
                if not self.cfg["risk"].get("perps_enabled"):
                    decisions = [d for d in decisions if d["action"] != "short"]
                if decisions:
                    return decisions
            except Exception:
                pass  # logged by caller; fall through to deterministic decider
        return self.fallback.decide(snapshot, signals, portfolio, risk_limits)


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
