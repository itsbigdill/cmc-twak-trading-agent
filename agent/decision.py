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

from .signal_engine import Regime, TokenSignal

SYSTEM_PROMPT = """\
Ти — компонент прийняття рішень автономного торгового агента на BNB Smart Chain.
Ти НЕ виконуєш угоди і НЕ рахуєш ціни — ти лише зважуєш уже обчислені сигнали
й повертаєш структуроване рішення.

ПРАВИЛА:
1. Поважай risk_limits. НІКОЛИ не пропонуй size_pct, що порушує max_position_pct.
2. Якщо daily_loss_remaining_pct близький до нуля — пропонуй лише hold/close.
3. Це турнір на ранг за дохідністю з жорстким drawdown-DQ: будь рішучим коли
   портфель здоровий, але миттєво деризикуй біля межі. Не all-in.
4. size_pct — частка від cash_usd, не від total_equity.
5. action може бути "short" (через перпи) у нисхідному тренді — не лише buy/sell.
6. confidence < 0.55 → action = "hold".
7. Завжди коротко пояснюй rationale з посиланням на конкретні сигнали.

ВИХІД — РІВНО цей JSON, без markdown:
{"decisions":[{"token":"CAKE","action":"buy|sell|short|hold|close","size_pct":0.0,
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


# --- LLM decider ---------------------------------------------------------------
class ClaudeDecider:
    def __init__(self, cfg: dict, fallback=None):
        self.cfg = cfg
        self.fallback = fallback or RuleBasedDecider(cfg)
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return self._client

    def decide(self, snapshot, signals, portfolio, risk_limits):
        payload = build_snapshot_payload(snapshot, signals, portfolio, risk_limits)
        user = (
            "Поточний ринковий зріз:\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Поверни рішення у заданій JSON-схемі. Тільки JSON."
        )
        try:
            resp = self._client_lazy().messages.create(
                model=self.cfg["llm"]["model"],
                max_tokens=self.cfg["llm"]["max_tokens"],
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            text = resp.content[0].text.strip()
            parsed = json.loads(text)
            decisions = _validate(parsed.get("decisions", []))
            if decisions:
                return decisions
        except Exception:
            pass  # logged by caller; fall through to deterministic decider
        return self.fallback.decide(snapshot, signals, portfolio, risk_limits)


def _dec(token, action, size_pct, confidence, rationale):
    return {"token": token, "action": action, "size_pct": round(size_pct, 4),
            "confidence": round(confidence, 4), "rationale": rationale}


def build_decider(cfg: dict):
    policy = cfg.get("decision", {}).get("policy", "threshold")
    base = RotationDecider(cfg) if policy == "rotation" else RuleBasedDecider(cfg)
    if cfg.get("mode") == "live" and os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeDecider(cfg, fallback=base)
    return base
