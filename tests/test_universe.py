import json

from agent import universe
from agent.universe import UniverseManager


def _cfg(tmp_path):
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({
        "GOOD": {"address": "0xgood", "ambiguous": False, "stable": False},
        "AMB": {"address": "0xamb", "ambiguous": True, "stable": False},
    }))
    return {
        "_config_dir": str(tmp_path), "quote_asset": "USDT",
        "regime": {"benchmark": "BTC"},
        "twak": {"chain": "bsc", "quote_contract": "0xusdt",
                 "token_contracts": {"CORE": "0xcore"}},
        "universe": {"resolved_contracts_file": "resolved.json",
                     "cache_file": "cache.json", "refresh_hours": 4,
                     "probe_usd": 4, "max_round_trip_loss_pct": 3,
                     "min_history_bars": 35, "max_assets": 60, "workers": 1},
    }


def test_refresh_adds_only_safe_round_trip_assets(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)

    def fake(args, timeout=45):
        if args[0] == "risk":
            return {"supportsSwap": True, "securityInfo": {"riskLevel": "low"}}
        if args[0] == "price":
            return {"history": [{"price": 1}] * 40}
        if args[0] == "swap" and "--usd" in args:
            return {"input": "4 USDT", "output": "10 GOOD"}
        return {"input": "10 GOOD", "output": "3.92 USDT"}

    monkeypatch.setattr(universe, "_twak", fake)
    manager = UniverseManager(cfg)
    assert manager.refresh(force=True)
    assert cfg["twak"]["token_contracts"] == {"CORE": "0xcore", "GOOD": "0xgood"}
    assert cfg["universe_runtime"]["GOOD"]["round_trip_loss_pct"] == 2.0


def test_refresh_rejects_bad_round_trip(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)

    def fake(args, timeout=45):
        if args[0] == "risk":
            return {"supportsSwap": True, "securityInfo": {"riskLevel": "low"}}
        if args[0] == "price":
            return {"history": [{"price": 1}] * 40}
        if args[0] == "swap" and "--usd" in args:
            return {"input": "4 USDT", "output": "10 GOOD"}
        return {"input": "10 GOOD", "output": "2 USDT"}

    monkeypatch.setattr(universe, "_twak", fake)
    manager = UniverseManager(cfg)
    assert manager.refresh(force=True)
    assert "GOOD" not in cfg["twak"]["token_contracts"]
