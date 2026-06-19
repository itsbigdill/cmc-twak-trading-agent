"""
Live signal source (the fix for the universe / timeframe / CMC-credit problems).

Per-token technicals (RSI/MACD/EMA) are computed from `twak price --history`
(hourly-scale bars — the right horizon for a 1-week competition) so:
  * every trade token gets a signal (no CMC numeric id needed),
  * signals actually move within the week (vs near-static daily CMC technicals),
  * we don't burn the CMC credit budget on per-token technical calls.

CMC Agent Hub still supplies the macro layer it's unique for — Fear & Greed and
BTC dominance (one cheap call/tick) — so "uses CMC signals" stays true.
"""

from __future__ import annotations

import json
import subprocess

from .cmc_client import CMCMCPClient, MockCMCClient, _dig, _num
from .indicators import signals_from_prices


class TwakCmcSignalClient:
    _BSC_COINID = "20000714"
    _RISK_SCORE = {"low": 10, "medium": 45, "high": 85}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.contracts = cfg["twak"]["token_contracts"]
        self.chain = cfg["twak"]["chain"]
        self.period = cfg.get("signal", {}).get("history_period", "week")
        self._risk_cache: dict[str, float] = {}    # token -> score (risk is ~static)
        self._llm = None                           # lazy anthropic client for news scoring
        try:
            self._cmc = CMCMCPClient(cfg["cmc"]["mcp_url"], ids=cfg["cmc"].get("token_ids"))
        except Exception:
            self._cmc = None                       # run with neutral macro if no key

    def token_risk_score(self, token: str) -> float:
        """0(safe)..100(risky) from `twak risk`; cached. Unknown -> 100 (block)."""
        addr = self.contracts.get(token)
        if not addr:
            return 0.0                             # benchmark/native, not traded
        if token in self._risk_cache:
            return self._risk_cache[token]
        score = 100.0
        try:
            out = subprocess.run(
                ["twak", "risk", f"c{self._BSC_COINID}_t{addr}", "--json"],
                capture_output=True, text=True, timeout=40)
            r = json.loads(out.stdout[out.stdout.find("{"):])
            if r.get("supportsSwap"):
                lvl = (r.get("securityInfo", {}) or {}).get("riskLevel", "high")
                score = self._RISK_SCORE.get(lvl, 85)
        except Exception:
            pass
        self._risk_cache[token] = score
        return score

    def _history(self, token: str) -> list[float]:
        ref = self.contracts.get(token)            # benchmark (BTC) resolves by symbol
        cmd = ["twak", "price", ref or token, "--history", self.period, "--json"]
        if ref:
            cmd += ["--chain", self.chain]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(out.stdout[out.stdout.find("{"):])
            return [h["price"] for h in data.get("history", [])]
        except Exception:
            return []

    def _macro_signals(self) -> dict:
        """Macro layer from CMC Agent Hub, computed once per snapshot:
          * Fear & Greed + BTC dominance  (get_global_metrics_latest)
          * real funding rate             (get_global_crypto_derivatives_metrics)
          * LLM-scored news sentiment     (get_crypto_latest_news)
        Three CMC tools; each component degrades to neutral on any error."""
        m = {"fear_greed_index": 50.0, "btc_dominance": 54.0,
             "funding_rate": 0.0, "news_sentiment": 0.0}
        if self._cmc is None:
            return m
        try:
            g = self._cmc.call_tool("get_global_metrics_latest", {}) or {}
            m["fear_greed_index"] = _num(_dig(g, "sentiment", "fear_greed", "current", "index", default=50))
            m["btc_dominance"] = _num(_dig(g, "dominance", "btc", "current", default="54%"))
        except Exception:
            pass
        m["funding_rate"] = self._funding_rate()
        m["news_sentiment"] = self._news_sentiment()
        return m

    def _funding_rate(self) -> float:
        """Global perp funding rate from CMC derivatives metrics (fundingRate.current).
        Contrarian downstream: crowded longs (high +funding) -> caution."""
        try:
            d = self._cmc.call_tool("get_global_crypto_derivatives_metrics", {}) or {}
            return _num(_dig(d, "fundingRate", "current", default=0.0))
        except Exception:
            return 0.0

    def _news_sentiment(self) -> float:
        """Market mood in [-1,1], LLM-scored from CMC's latest BTC headlines via
        whichever LLM provider is configured (Gemini or Claude). Fully optional:
        no key or any error -> 0.0 (zero effect on the score)."""
        from . import llm
        if not llm.available():
            return 0.0
        try:
            d = self._cmc.call_tool("get_crypto_latest_news", {"id": "1"}) or {}
            rows = d.get("rows", []) if isinstance(d, dict) else []
            titles = [r[0] for r in rows[:8] if isinstance(r, list) and r]
            if not titles:
                return 0.0
            prompt = ("Score the overall crypto-market sentiment of these headlines "
                      "from -1 (very bearish) to +1 (very bullish). Reply with ONLY "
                      "the number.\n\n" + "\n".join(f"- {t}" for t in titles))
            text = llm.complete(prompt, max_tokens=16, timeout=20.0)
            import re
            mtch = re.search(r"-?\d*\.?\d+", text or "")
            return max(-1.0, min(1.0, float(mtch.group()))) if mtch else 0.0
        except Exception:
            return 0.0

    def get_snapshot(self, tokens: list[str]) -> dict[str, dict]:
        from concurrent.futures import ThreadPoolExecutor
        macro = self._macro_signals()
        toks = [t for t in tokens if t != self.cfg["quote_asset"]]
        # Fetch per-token price history CONCURRENTLY: serial × 30s × ~22 tokens
        # could exceed the 15-min tick. Bounded pool keeps a full snapshot well
        # under one tick even if several names are slow.
        with ThreadPoolExecutor(max_workers=8) as ex:
            hist = dict(zip(toks, ex.map(self._history, toks)))
        news = macro.get("news_sentiment", 0.0)
        snap: dict[str, dict] = {}
        for t in toks:
            prices = hist.get(t) or []
            if len(prices) < 2:
                continue                           # no data -> skip (off-universe/illiquid)
            snap[t] = {"price": prices[-1], **signals_from_prices(prices),
                       **macro, "news_sentiment": news,
                       "token_risk_score": self.token_risk_score(t)}
        return snap


def build_signal_source(cfg: dict):
    """live/paper -> real TWAK price-history TA + CMC macro; dry_run -> mock."""
    if cfg.get("mode") in ("live", "paper"):
        return TwakCmcSignalClient(cfg)
    return MockCMCClient()
