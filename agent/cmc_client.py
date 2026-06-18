"""
CMC Agent Hub client (F1) — the agent's eyes.

Two implementations behind one interface so the whole pipeline runs today:
  * MockCMCClient    — deterministic synthetic data; no network/key needed.
  * CMCMCPClient     — real CoinMarketCap MCP (JSON-RPC over HTTP).

`get_snapshot(tokens)` returns, per token:
  { price, rsi, macd_state, ema_trend, fear_greed_index, btc_dominance,
    news_sentiment }
matching exactly what signal_engine expects.

NOTE on the real client: the MCP transport (initialize -> tools/call) is wired,
but the 12 tool NAMES and their argument/response shapes must be confirmed
against the live server's tools/list (see _TOOL_NAMES). Confirm in the Builder
Telegram / by calling list_tools() once, then fill the mapping. Everything else
(signal, risk, executor, loop, reporting) is independent of this detail.
"""

from __future__ import annotations

import os
from typing import Protocol


class CMCClient(Protocol):
    def get_snapshot(self, tokens: list[str]) -> dict[str, dict]: ...


# --- Mock (offline / dry-run) --------------------------------------------------
class MockCMCClient:
    """Synthetic but internally-consistent market data for offline testing.

    Uses a seed so a given tick is reproducible. Produces a mild uptrend on BTC
    so the regime is well-defined in demos.
    """

    _BASE_PRICES = {"BNB": 600.0, "CAKE": 2.4, "ETH": 3500.0, "BTC": 65000.0, "USDT": 1.0}

    def __init__(self, seed: int | None = None):
        # None -> time-varying so consecutive runs differ; int -> reproducible.
        self.seed = seed
        self._tick = 0
        self._prices: dict[str, float] = {}   # coherent random walk across ticks

    def get_snapshot(self, tokens: list[str]) -> dict[str, dict]:
        import random
        import time as _time

        base = self.seed if self.seed is not None else int(_time.time())
        rng = random.Random(base + self._tick)
        self._tick += 1
        fg = rng.randint(30, 70)
        btc_dom = round(rng.uniform(50, 58), 2)
        snap: dict[str, dict] = {}
        trends = ["up", "down", "flat"]
        macds = ["bullish_cross", "bullish", "neutral", "bearish", "bearish_cross"]
        for t in tokens:
            # coherent price: small drift + noise on the previous price
            px = self._prices.get(t, self._BASE_PRICES.get(t, 100.0))
            drift = 0.002 if t == "BTC" else rng.uniform(-0.01, 0.01)
            px = max(0.01, px * (1 + drift + rng.uniform(-0.02, 0.02)))
            self._prices[t] = px

            if t == "BTC":
                ema, macd = "up", "bullish"      # define the regime for demos
            else:
                ema, macd = rng.choice(trends), rng.choice(macds)
            snap[t] = {
                "price": round(px, 4),
                "rsi": round(rng.uniform(20, 80), 1),
                "macd_state": macd,
                "ema_trend": ema,
                "fear_greed_index": fg,
                "btc_dominance": btc_dom,
                "news_sentiment": round(rng.uniform(-0.5, 0.5), 2),
            }
        return snap


# --- Real CMC MCP client -------------------------------------------------------
# Confirm these names via list_tools() against the live server before going live.
_TOOL_NAMES = {
    "quote": "get_quotes_latest",
    "technicals": "get_technical_indicators",
    "global": "get_global_metrics",
    "news": "get_latest_news",
}


class CMCMCPClient:
    def __init__(self, mcp_url: str, api_key: str | None = None, timeout: float = 30.0):
        import httpx

        self.url = mcp_url
        self.api_key = api_key or os.environ.get("CMC_MCP_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("CMC_MCP_API_KEY not set")
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-CMC-MCP-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        self._id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        resp = self._client.post(self.url, json=payload)
        resp.raise_for_status()
        # MCP may stream SSE; for simple request/response servers JSON is returned.
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", {})

    def list_tools(self) -> list[dict]:
        """Call once to discover real tool names; then fix _TOOL_NAMES."""
        return self._rpc("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def get_snapshot(self, tokens: list[str]) -> dict[str, dict]:
        """Assemble the per-token snapshot from the relevant MCP tools.

        Left as a thin orchestration over call_tool so the exact field paths can
        be adjusted once the live response shapes are confirmed.
        """
        glob = self.call_tool(_TOOL_NAMES["global"], {})
        fg = _dig(glob, "fear_and_greed", "value", default=50)
        btc_dom = _dig(glob, "btc_dominance", default=54.0)

        snap: dict[str, dict] = {}
        for t in tokens:
            quote = self.call_tool(_TOOL_NAMES["quote"], {"symbol": t})
            tech = self.call_tool(_TOOL_NAMES["technicals"], {"symbol": t})
            news = self.call_tool(_TOOL_NAMES["news"], {"symbol": t, "limit": 5})
            snap[t] = {
                "price": _dig(quote, "price", default=0.0),
                "rsi": _dig(tech, "rsi", default=50.0),
                "macd_state": _dig(tech, "macd_state", default="neutral"),
                "ema_trend": _dig(tech, "ema_trend", default="flat"),
                "fear_greed_index": fg,
                "btc_dominance": btc_dom,
                "news_sentiment": _dig(news, "sentiment", default=0.0),
            }
        return snap


def _dig(obj, *keys, default=None):
    """Safely walk nested dicts; returns default if any key is missing."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def build_cmc_client(cfg: dict) -> CMCClient:
    if cfg.get("mode") == "live":
        return CMCMCPClient(cfg["cmc"]["mcp_url"])
    return MockCMCClient()
