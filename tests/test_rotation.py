"""Tests for the cross-sectional rotation decider."""

from agent.decision import RotationDecider
from agent.signal_engine import Regime, TokenSignal


def _sig(token, score, regime):
    return TokenSignal(token=token, score=score, regime=regime, components={}, actionable=True)


def _portfolio(positions=None):
    return {"cash_usd": 1000, "total_equity_usd": 1000, "positions": positions or {}}


def test_rotation_picks_top_k_in_uptrend(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 2, "rotation_min_momentum": 0.05}}
    d = RotationDecider(cfg)
    signals = {
        "ETH": _sig("ETH", 0.8, Regime.TREND_UP),
        "CAKE": _sig("CAKE", 0.5, Regime.TREND_UP),
        "LINK": _sig("LINK", 0.1, Regime.TREND_UP),
    }
    out = d.decide({}, signals, _portfolio(), {})
    buys = [x for x in out if x["action"] == "buy"]
    assert {b["token"] for b in buys} == {"ETH", "CAKE"}   # top 2 by momentum


def test_rotation_downtrend_holds_strong_exits_weak(cfg):
    # exercise the counter-trend code path regardless of the config default
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_downtrend_topk": 2,
                               "rotation_downtrend_min_momentum": 0.2}}
    d = RotationDecider(cfg)
    # downtrend: a strongly-positive name is ridden (counter-trend), a weak one cut
    signals = {"ETH": _sig("ETH", 0.8, Regime.TREND_DOWN),   # strong -> keep
               "CAKE": _sig("CAKE", 0.05, Regime.TREND_DOWN)}  # weak -> exit
    out = d.decide({}, signals, _portfolio({"CAKE": 5.0}), {})
    actions = {(x["token"], x["action"]) for x in out}
    assert ("CAKE", "close") in actions                     # weak name exited
    assert ("ETH", "buy") in actions                        # strongest name entered


def test_rotation_downtrend_all_weak_goes_cash(cfg):
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.05, Regime.TREND_DOWN)}  # below downtrend threshold
    out = d.decide({}, signals, _portfolio({"ETH": 5.0}), {})
    assert all(x["action"] == "close" for x in out)         # nothing strong -> cash


def test_rotation_holds_in_chop(cfg):
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.8, Regime.CHOP)}
    out = d.decide({}, signals, _portfolio({"ETH": 5.0}), {})
    assert out == []                                       # no churn in chop


def test_rotation_rotates_out_losers(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 1, "rotation_min_momentum": 0.05}}
    d = RotationDecider(cfg)
    # hold CAKE but ETH is now strongest -> close CAKE, buy ETH
    signals = {
        "ETH": _sig("ETH", 0.9, Regime.TREND_UP),
        "CAKE": _sig("CAKE", 0.2, Regime.TREND_UP),
    }
    out = d.decide({}, signals, _portfolio({"CAKE": 10.0}), {})
    actions = {(x["token"], x["action"]) for x in out}
    assert ("CAKE", "close") in actions
    assert ("ETH", "buy") in actions


def test_rotation_ignores_non_tradeable(cfg):
    d = RotationDecider(cfg)   # BTC not in token_contracts
    signals = {"BTC": _sig("BTC", 0.9, Regime.TREND_UP)}
    out = d.decide({}, signals, _portfolio(), {})
    assert out == []
