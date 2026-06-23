"""Periodic, execution-aware refresh of the eligible BSC trade universe."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor


_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _number(value) -> float:
    match = _NUM.search(str(value or ""))
    return float(match.group()) if match else 0.0


def _twak(args: list[str], timeout: float = 45.0) -> dict:
    try:
        proc = subprocess.run(["twak", *args, "--json"], capture_output=True,
                              text=True, timeout=timeout)
        out = proc.stdout or ""
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        data = json.loads(out[min(starts):]) if starts else {}
        if proc.returncode or data.get("error"):
            return {}
        return data
    except Exception:
        return {}


class UniverseManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        u = cfg.get("universe", {})
        root = cfg.get("_config_dir") or os.getcwd()
        self.resolved_file = os.path.join(root, u.get("resolved_contracts_file",
                                                       "config/bsc_contracts.json"))
        self.cache_file = os.path.join(root, u.get("cache_file", "state/universe_cache.json"))
        self.refresh_seconds = float(u.get("refresh_hours", 4)) * 3600
        self.stale_seconds = float(u.get("retain_last_good_hours", 12)) * 3600
        self.probe_usd = float(u.get("probe_usd", 4.0))
        self.max_round_trip_loss = float(u.get("max_round_trip_loss_pct", 3.0))
        self.max_assets = int(u.get("max_assets", 60))
        self.min_bars = int(u.get("min_history_bars", 35))
        self.workers = int(u.get("workers", 8))
        self.base = dict(cfg.get("_static_contracts") or cfg["twak"]["token_contracts"])
        self.last_refresh = 0.0
        self._load_cache()

    def _apply(self, assets: dict, metrics: dict, refreshed_at: float) -> None:
        # Mutate the existing mapping so the already-created executor sees updates.
        contracts = self.cfg["twak"]["token_contracts"]
        contracts.clear()
        contracts.update(self.base)
        contracts.update(assets)
        self.cfg["universe_runtime"] = metrics
        bench = self.cfg["regime"]["benchmark"]
        self.cfg["whitelist"] = ([bench] + [t for t in contracts if t != bench]
                                  + [self.cfg["quote_asset"]])
        self.last_refresh = refreshed_at

    def _load_cache(self) -> None:
        try:
            data = json.load(open(self.cache_file))
            self._apply(data.get("assets", {}), data.get("metrics", {}),
                        float(data.get("refreshed_at", 0)))
        except Exception:
            pass

    def _probe(self, item) -> tuple[str, str, dict] | None:
        symbol, meta = item
        if meta.get("stable") or meta.get("ambiguous"):
            return None
        address = meta.get("address")
        if not address:
            return None
        risk = _twak(["risk", f"c20000714_t{address}"])
        level = (risk.get("securityInfo") or {}).get("riskLevel", "high")
        if not risk.get("supportsSwap") or level == "high":
            return None
        hist = _twak(["price", address, "--chain", "bsc", "--history", "week"])
        if len(hist.get("history") or []) < self.min_bars:
            return None
        buy = _twak(["swap", "--usd", str(self.probe_usd), self.cfg["quote_asset"],
                     address, "--chain", self.cfg["twak"]["chain"], "--quote-only"])
        qty = _number(buy.get("output"))
        spent = _number(buy.get("input"))
        if qty <= 0 or spent <= 0:
            return None
        sell = _twak(["swap", format(qty, ".18f"), address,
                      self.cfg["twak"].get("quote_contract", self.cfg["quote_asset"]),
                      "--chain", self.cfg["twak"]["chain"], "--quote-only"])
        recovered = _number(sell.get("output"))
        if recovered <= 0:
            return None
        loss = max(0.0, (1.0 - recovered / spent) * 100.0)
        if loss > self.max_round_trip_loss:
            return None
        history = [float(x["price"]) for x in (hist.get("history") or []) if x.get("price")]
        return symbol, address, {"round_trip_loss_pct": round(loss, 4),
                                 "risk_level": level,
                                 "history_bars": len(history),
                                 "history": history,
                                 "validated_at": time.time()}

    def refresh(self, *, force: bool = False) -> bool:
        now = time.time()
        if not force and self.last_refresh and now - self.last_refresh < self.refresh_seconds:
            return False
        try:
            resolved = json.load(open(self.resolved_file))
        except Exception:
            return False
        previous_assets = {s: a for s, a in self.cfg["twak"]["token_contracts"].items()
                           if s not in self.base}
        previous_metrics = dict(self.cfg.get("universe_runtime", {}))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            rows = [r for r in pool.map(self._probe, resolved.items()) if r]
        # Lowest friction first; the static core is always retained separately.
        rows.sort(key=lambda r: (r[2]["round_trip_loss_pct"], r[0]))
        rows = rows[:self.max_assets]
        assets = {symbol: address for symbol, address, _ in rows}
        metrics = {symbol: meta for symbol, _, meta in rows}
        # A transient TWAK/RPC miss must not evict a recently validated asset.
        for symbol, address in previous_assets.items():
            meta = previous_metrics.get(symbol) or {}
            if symbol not in assets and now - float(meta.get("validated_at", 0)) <= self.stale_seconds:
                assets[symbol] = address
                metrics[symbol] = meta
        self._apply(assets, metrics, now)
        os.makedirs(os.path.dirname(self.cache_file) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.cache_file) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"refreshed_at": now, "assets": assets, "metrics": metrics}, f, indent=2)
        os.replace(tmp, self.cache_file)
        return True
