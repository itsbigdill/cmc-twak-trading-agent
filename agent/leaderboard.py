"""Best-effort tournament rank tracking; trading remains functional if unavailable."""

from __future__ import annotations

import json
import re
import time
import urllib.request


_cache = {"at": 0.0, "data": {}}


def current_status(cfg: dict) -> dict:
    lb = cfg.get("leaderboard", {})
    url = lb.get("url")
    if not url:
        return {}
    now = time.time()
    if now - _cache["at"] < float(lb.get("refresh_minutes", 30)) * 60:
        return dict(_cache["data"])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CMC-TWAK-Agent/1.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode()
        match = re.search(r"const D=(\{.*?\}), R=D\.rows", html, re.S)
        data = json.loads(match.group(1)) if match else {}
        rows = [r for r in data.get("rows", []) if r.get("traded") and r.get("value", 0) >= 1]
        rows.sort(key=lambda r: -(r.get("ret_pct") if r.get("ret_pct") is not None else -1e9))
        address = cfg["twak"].get("agent_address", "").lower()
        status = next(({"rank": i, "return_pct": r.get("ret_pct"),
                        "drawdown_pct": r.get("dd_pct"), "value_usd": r.get("value"),
                        "trades": r.get("trades"), "holdings": r.get("holds", []),
                        "top5_return_pct": rows[4].get("ret_pct") if len(rows) >= 5 else None,
                        "leaderboard_generated_ts": data.get("generated_ts") or data.get("built_ts")}
                       for i, r in enumerate(rows, 1) if r.get("agent", "").lower() == address), {})
        _cache.update(at=now, data=status)
        return dict(status)
    except Exception:
        _cache["at"] = now
        return dict(_cache["data"])
