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
import math
import os
import statistics
import subprocess
import time

from .cmc_client import CMCMCPClient, MockCMCClient, _dig, _num
from .cmc_client import derive_ema_trend, derive_macd_state
from .indicators import signals_from_prices
from .universe import UniverseManager


def market_features(prices: list[float]) -> dict[str, float]:
    """Short-horizon, scale-free features for cross-sectional tournament ranking."""
    if len(prices) < 3 or prices[-1] <= 0:
        return {"return_6h": 0.0, "return_24h": 0.0, "volatility_24h": 1.0,
                "vol_adjusted_return": 0.0, "distance_from_48h_high": -1.0}

    def ret(bars: int) -> float:
        old = prices[-min(len(prices), bars + 1)]
        return prices[-1] / old - 1.0 if old > 0 else 0.0

    tail = prices[-9:]
    log_rets = [math.log(b / a) for a, b in zip(tail, tail[1:]) if a > 0 and b > 0]
    vol = statistics.pstdev(log_rets) if len(log_rets) >= 2 else 0.0
    r6, r24 = ret(2), ret(8)              # TWAK week history is ~3-hour bars
    momentum = 0.4 * r6 + 0.6 * r24
    high = max(prices[-17:])
    return {
        "return_6h": r6,
        "return_24h": r24,
        "volatility_24h": vol,
        "vol_adjusted_return": momentum / max(vol, 0.01),
        "distance_from_48h_high": prices[-1] / high - 1.0 if high > 0 else -1.0,
    }


class TwakCmcSignalClient:
    _BSC_COINID = "20000714"
    _RISK_SCORE = {"low": 10, "medium": 45, "high": 85}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.contracts = cfg["twak"]["token_contracts"]
        self.chain = cfg["twak"]["chain"]
        self.period = cfg.get("signal", {}).get("history_period", "week")
        self._risk_cache: dict[str, float] = {}    # token -> score (risk is ~static)
        self._history_cache: dict[str, list[float]] = {}
        self._cmc_cache = self._load_cmc_cache()
        self._llm = None                           # lazy anthropic client for news scoring
        self._universe = UniverseManager(cfg) if cfg.get("universe", {}).get("enabled") else None
        try:
            self._cmc = CMCMCPClient(cfg["cmc"]["mcp_url"], ids=cfg["cmc"].get("token_ids"))
        except Exception:
            self._cmc = None                       # run with neutral macro if no key

    def _load_cmc_cache(self) -> dict:
        path = self.cfg.get("cmc", {}).get("cache_file", "state/cmc_enrichment_cache.json")
        root = self.cfg.get("_config_dir") or os.getcwd()
        self._cmc_cache_file = path if os.path.isabs(path) else os.path.join(root, path)
        try:
            data = json.load(open(self._cmc_cache_file))
            if isinstance(data, dict):
                data.setdefault("ids", {})
                data.setdefault("ambiguous", {})
                data.setdefault("quotes", {})
                data.setdefault("technicals", {})
                data.setdefault("metrics", {})
                data.setdefault("news", {})
                return data
        except Exception:
            pass
        return {"ids": dict(self.cfg.get("cmc", {}).get("token_ids") or {}),
                "ambiguous": {}, "quotes": {}, "technicals": {}, "metrics": {}, "news": {}}

    def _save_cmc_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cmc_cache_file) or ".", exist_ok=True)
            tmp = self._cmc_cache_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._cmc_cache, f, indent=2, sort_keys=True)
            os.replace(tmp, self._cmc_cache_file)
        except Exception:
            pass

    def maybe_refresh_universe(self) -> bool:
        if self._universe is None:
            return False
        changed = self._universe.refresh()
        # The contracts mapping is mutated in place, but keep this alias explicit.
        self.contracts = self.cfg["twak"]["token_contracts"]
        return changed

    def token_risk_score(self, token: str) -> float:
        """0(safe)..100(risky) from `twak risk`; cached. Unknown -> 100 (block)."""
        addr = self.contracts.get(token)
        if not addr:
            return 0.0                             # benchmark/native, not traded
        if token in self._risk_cache:
            return self._risk_cache[token]
        cached_level = (self.cfg.get("universe_runtime", {}).get(token) or {}).get("risk_level")
        if cached_level:
            score = self._RISK_SCORE.get(cached_level, 85)
            self._risk_cache[token] = score
            return score
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
            prices = [h["price"] for h in data.get("history", [])]
            if prices:
                self._history_cache[token] = prices
                return prices
        except Exception:
            pass
        cached = self._history_cache.get(token)
        if cached:
            return cached
        return list((self.cfg.get("universe_runtime", {}).get(token) or {}).get("history", []))

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

    @staticmethod
    def _rows_table(obj) -> list[dict]:
        """Normalize CMC table-shaped responses into a list of dicts."""
        if isinstance(obj, list):
            return [x for x in obj if isinstance(x, dict)]
        if not isinstance(obj, dict):
            return []
        rows = obj.get("rows")
        headers = obj.get("headers")
        if isinstance(rows, list) and isinstance(headers, list):
            out = []
            for row in rows:
                if isinstance(row, list):
                    out.append({str(k): v for k, v in zip(headers, row)})
                elif isinstance(row, dict):
                    out.append(row)
            return out
        return [obj]

    def _resolve_cmc_id(self, token: str) -> str | None:
        ids = self._cmc_cache.setdefault("ids", {})
        if token in ids:
            return str(ids[token])
        if self._cmc is None:
            return None
        try:
            data = self._cmc.call_tool("search_cryptos", {"query": token, "limit": 5})
            rows = self._rows_table(data)
            exact = [r for r in rows if str(r.get("symbol", "")).upper() == token.upper()]
            if len(exact) == 1:
                cid = str(exact[0].get("id") or "")
                if cid:
                    ids[token] = cid
                    return cid
            if exact:
                # Prefer the highest-ranked exact-symbol row, but mark ambiguity so
                # strategy can penalize possible symbol collisions.
                exact.sort(key=lambda r: int(r.get("rank") or 10**9))
                cid = str(exact[0].get("id") or "")
                if cid:
                    ids[token] = cid
                    self._cmc_cache.setdefault("ambiguous", {})[token] = True
                    return cid
        except Exception:
            return None
        return None

    def _ensure_cmc_ids(self, tokens: list[str]) -> dict[str, str]:
        if self._cmc is None or not self.cfg.get("cmc", {}).get("enrichment_enabled", True):
            return {}
        # Resolve slowly: this runs inside the live tick, so do not spend the whole
        # tick on a first-time 100-token symbol search. Cached ids persist.
        limit = int(self.cfg.get("cmc", {}).get("max_id_resolves_per_tick", 12))
        out = {}
        missing = []
        for token in tokens:
            cid = self._cmc_cache.get("ids", {}).get(token)
            if cid:
                out[token] = str(cid)
            else:
                missing.append(token)
        for token in missing[:limit]:
            cid = self._resolve_cmc_id(token)
            if cid:
                out[token] = cid
        return out

    def _cmc_quotes(self, ids_by_token: dict[str, str]) -> dict[str, dict]:
        now = time.time()
        ttl = float(self.cfg.get("cmc", {}).get("quotes_ttl_seconds", 900))
        cache = self._cmc_cache.setdefault("quotes", {})
        fresh = {
            t: v.get("data", {})
            for t, v in cache.items()
            if isinstance(v, dict) and now - float(v.get("ts", 0)) < ttl
        }
        need = {t: cid for t, cid in ids_by_token.items() if t not in fresh}
        if self._cmc is not None and need:
            ids = ",".join(dict.fromkeys(need.values()))
            try:
                data = self._cmc.call_tool("get_crypto_quotes_latest", {"id": ids})
                id_to_row = {str(r.get("id")): r for r in self._rows_table(data)}
                for token, cid in need.items():
                    row = id_to_row.get(str(cid))
                    if not row:
                        continue
                    parsed = self._parse_cmc_quote(row)
                    cache[token] = {"ts": now, "data": parsed}
                    fresh[token] = parsed
            except Exception:
                pass
        return fresh

    def _parse_cmc_quote(self, row: dict) -> dict:
        def pct(name: str) -> float:
            return _num(row.get(name, 0.0)) / 100.0
        return {
            "cmc_id": str(row.get("id") or ""),
            "cmc_name": row.get("name"),
            "cmc_slug": row.get("slug"),
            "cmc_rank": int(row.get("rank") or 0),
            "cmc_price": _num(row.get("price", 0.0)),
            "cmc_market_cap": _num(row.get("market_cap", 0.0)),
            "cmc_volume_24h": _num(row.get("volume_24h", 0.0)),
            "cmc_volume_change_24h": pct("volume_change_24h"),
            "cmc_turnover": _num(row.get("turnover", 0.0)),
            "cmc_pct_1h": pct("percent_change_1h"),
            "cmc_pct_24h": pct("percent_change_24h"),
            "cmc_pct_7d": pct("percent_change_7d"),
            "cmc_pct_30d": pct("percent_change_30d"),
        }

    def _candidate_tokens_for_deep_cmc(self, snap: dict[str, dict]) -> list[str]:
        n = int(self.cfg.get("cmc", {}).get("enrich_top_n", 8))
        if n <= 0:
            return []

        def preliminary(token: str) -> float:
            d = snap[token]
            cmc = d.get("cmc_quote", {})
            r6 = float(d.get("return_6h", 0.0))
            r24 = float(d.get("return_24h", 0.0))
            vol_adj = max(-1.0, min(1.0, float(d.get("vol_adjusted_return", 0.0)) / 3.0))
            c1 = float(cmc.get("cmc_pct_1h", 0.0))
            c24 = float(cmc.get("cmc_pct_24h", 0.0))
            c7 = float(cmc.get("cmc_pct_7d", 0.0))
            vol_chg = max(-1.0, min(1.0, float(cmc.get("cmc_volume_change_24h", 0.0)) / 2.0))
            return 0.25 * r6 + 0.35 * r24 + 0.15 * vol_adj + 0.10 * c1 + 0.20 * c24 + 0.05 * c7 + 0.05 * vol_chg

        return sorted(snap, key=preliminary, reverse=True)[:n]

    def _cached_tool(self, bucket: str, token: str, tool: str, args: dict, ttl: float):
        now = time.time()
        cache = self._cmc_cache.setdefault(bucket, {})
        cur = cache.get(token)
        if isinstance(cur, dict) and now - float(cur.get("ts", 0)) < ttl:
            return cur.get("data")
        if self._cmc is None:
            return cur.get("data") if isinstance(cur, dict) else None
        try:
            data = self._cmc.call_tool(tool, args)
            cache[token] = {"ts": now, "data": data}
            return data
        except Exception:
            return cur.get("data") if isinstance(cur, dict) else None

    def _token_news_sentiment(self, token: str, cmc_id: str) -> float:
        from . import llm
        if not llm.available():
            return 0.0
        ttl = float(self.cfg.get("cmc", {}).get("news_ttl_seconds", 7200))
        data = self._cached_tool("news", token, "get_crypto_latest_news",
                                 {"id": cmc_id, "limit": 8}, ttl)
        rows = data.get("rows", []) if isinstance(data, dict) else []
        titles = []
        for row in rows[:8]:
            if isinstance(row, list) and row:
                titles.append(str(row[0]))
            elif isinstance(row, dict) and row.get("title"):
                titles.append(str(row["title"]))
        if not titles:
            return 0.0
        try:
            prompt = ("Score token-specific news sentiment for " + token +
                      " from -1 (very bearish/risky) to +1 (very bullish). "
                      "Reply with ONLY the number.\n\n" +
                      "\n".join(f"- {t}" for t in titles))
            text = llm.complete(prompt, max_tokens=16, timeout=20.0)
            import re
            mtch = re.search(r"-?\d*\.?\d+", text or "")
            return max(-1.0, min(1.0, float(mtch.group()))) if mtch else 0.0
        except Exception:
            return 0.0

    def _parse_cmc_technical(self, data) -> dict:
        if not isinstance(data, dict):
            return {}
        rsi = _num(_dig(data, "rsi", "rsi14", default=50.0), 50.0)
        macd = derive_macd_state(data.get("macd", {}) or {})
        ema = derive_ema_trend(data.get("moving_averages", {}) or {})
        pivot = _num(data.get("pivotPoint", 0.0))
        return {"cmc_rsi14": rsi, "cmc_macd_state": macd, "cmc_ema_trend": ema,
                "cmc_pivot": pivot}

    def _parse_cmc_metrics(self, data) -> dict:
        if not isinstance(data, dict):
            return {}
        holder = data.get("coinMarketCapCryptoHolderData") or {}
        total = data.get("coinMarketCapCryptoTotalHolderData") or {}
        dist = data.get("circulatingSupplyDistribution") or {}
        whales = dist.get("whales") or {}
        top10 = _num(holder.get("top10HolderBalancePercent", whales.get("percentOfSupply", 0.0))) / 100.0
        holder_30d = _num(total.get("cryptoTotalHolderCount30dChangePercent", 0.0)) / 100.0
        mcap_holder_30d = _num(total.get("cryptoHolderMarketCapUsd30dChangePercent", 0.0)) / 100.0
        return {"cmc_top10_holder_pct": top10,
                "cmc_holder_count_30d": holder_30d,
                "cmc_holder_mcap_30d": mcap_holder_30d}

    def _cmc_score(self, d: dict) -> float:
        q = d.get("cmc_quote", {})
        if not q:
            return 0.0
        mom = (0.20 * max(-1.0, min(1.0, float(q.get("cmc_pct_1h", 0.0)) / 0.05)) +
               0.40 * max(-1.0, min(1.0, float(q.get("cmc_pct_24h", 0.0)) / 0.15)) +
               0.20 * max(-1.0, min(1.0, float(q.get("cmc_pct_7d", 0.0)) / 0.35)) +
               0.10 * max(-1.0, min(1.0, float(q.get("cmc_volume_change_24h", 0.0)) / 1.5)))
        vol = float(q.get("cmc_volume_24h", 0.0))
        if vol < float(self.cfg.get("cmc", {}).get("min_volume_24h_usd", 500_000)):
            mom -= 0.25
        tech = 0.0
        if d.get("cmc_macd_state") == "bullish":
            tech += 0.25
        elif d.get("cmc_macd_state") == "bearish":
            tech -= 0.25
        if d.get("cmc_ema_trend") == "up":
            tech += 0.25
        elif d.get("cmc_ema_trend") == "down":
            tech -= 0.25
        rsi = float(d.get("cmc_rsi14", 50.0))
        if rsi >= 75:
            tech -= 0.25
        elif rsi <= 35:
            tech += 0.10
        whale = float(d.get("cmc_top10_holder_pct", 0.0))
        whale_penalty = max(0.0, whale - 0.80) * 1.5
        holder = max(-0.25, min(0.25, float(d.get("cmc_holder_count_30d", 0.0))))
        news = float(d.get("token_news_sentiment", 0.0))
        ambiguous = 0.20 if d.get("cmc_ambiguous") else 0.0
        return max(-1.0, min(1.0, mom + tech + 0.15 * holder + 0.20 * news - whale_penalty - ambiguous))

    def _apply_cmc_enrichment(self, snap: dict[str, dict]) -> None:
        if self._cmc is None or not self.cfg.get("cmc", {}).get("enrichment_enabled", True):
            return
        # Spend first-run CMC symbol searches on likely candidates first, not on
        # static whitelist order. The rest of the universe is filled gradually by
        # the persistent id cache.
        prioritized = self._candidate_tokens_for_deep_cmc(snap)
        prioritized += [t for t in snap if t not in prioritized]
        ids = self._ensure_cmc_ids(prioritized)
        quotes = self._cmc_quotes(ids)
        ambiguous = self._cmc_cache.get("ambiguous", {})
        for token, d in snap.items():
            q = quotes.get(token)
            if not q:
                continue
            d["cmc_quote"] = q
            d.update(q)
            d["cmc_ambiguous"] = bool(ambiguous.get(token))
        deep = self._candidate_tokens_for_deep_cmc(snap)
        tech_ttl = float(self.cfg.get("cmc", {}).get("technicals_ttl_seconds", 3600))
        metrics_ttl = float(self.cfg.get("cmc", {}).get("metrics_ttl_seconds", 21600))
        for token in deep:
            cid = ids.get(token)
            if not cid:
                continue
            tech = self._cached_tool("technicals", token, "get_crypto_technical_analysis",
                                     {"id": cid}, tech_ttl)
            metrics = self._cached_tool("metrics", token, "get_crypto_metrics",
                                        {"id": cid}, metrics_ttl)
            snap[token].update(self._parse_cmc_technical(tech))
            snap[token].update(self._parse_cmc_metrics(metrics))
            snap[token]["token_news_sentiment"] = self._token_news_sentiment(token, cid)
        for d in snap.values():
            d["cmc_score"] = round(self._cmc_score(d), 4)
        self._save_cmc_cache()

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
                       **market_features(prices),
                       **macro, "news_sentiment": news,
                       "token_risk_score": self.token_risk_score(t),
                       "round_trip_loss_pct": float(
                           (self.cfg.get("universe_runtime", {}).get(t) or {}).get(
                               "round_trip_loss_pct", 0.0))}
        self._apply_cmc_enrichment(snap)
        return snap


def build_signal_source(cfg: dict):
    """live/paper -> real TWAK price-history TA + CMC macro; dry_run -> mock."""
    if cfg.get("mode") in ("live", "paper"):
        return TwakCmcSignalClient(cfg)
    return MockCMCClient()
