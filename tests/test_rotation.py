"""Tests for the cross-sectional rotation decider."""

import pytest

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


def test_rotation_hurdle_keeps_nearly_equal_held_name(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 1,
                               "rotation_score_hurdle": 0.13}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.60, Regime.TREND_UP),
               "CAKE": _sig("CAKE", 0.55, Regime.TREND_UP)}
    out = d.decide({}, signals, _portfolio({"CAKE": 5.0}), {})
    assert not any(x["token"] == "CAKE" and x["action"] == "close" for x in out)


def test_rotation_targets_sixty_percent_gross_across_two_names(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 2,
                               "target_gross_exposure_pct": 0.60,
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.8, Regime.TREND_UP),
               "CAKE": _sig("CAKE", 0.7, Regime.TREND_UP)}
    out = d.decide({}, signals, _portfolio(), {})
    buys = [x for x in out if x["action"] == "buy"]
    assert len(buys) == 2
    assert all(x["size_pct"] == pytest.approx(0.30) for x in buys)


def test_divergent_leaderboard_mark_does_not_trigger_top5_mode(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 2,
                               "target_gross_exposure_pct": 0.60,
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.8, Regime.TREND_UP),
               "CAKE": _sig("CAKE", 0.7, Regime.TREND_UP)}
    risk = {"leaderboard_rank": 2, "leaderboard_return_pct": 10.0,
            "executable_return_pct": -9.0, "signal_streaks": {"ETH": 2, "CAKE": 2}}
    buys = [x for x in d.decide({}, signals, _portfolio(), risk) if x["action"] == "buy"]
    assert len(buys) == 2
    assert all(x["size_pct"] == pytest.approx(0.30) for x in buys)


def test_top5_mode_emits_trim_for_excess_existing_position(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "top5_gross_exposure_pct": 0.15}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.8, Regime.TREND_UP)}
    p = _portfolio({"ETH": 10.0})
    p["total_equity_usd"] = 100.0
    p["cash_usd"] = 60.0
    p["position_values"] = {"ETH": 40.0}
    risk = {"leaderboard_rank": 2, "leaderboard_return_pct": 5.0,
            "executable_return_pct": 3.0, "signal_streaks": {"ETH": 2}}
    out = d.decide({}, signals, p, risk)
    trim = next(x for x in out if x["action"] == "trim")
    assert trim["size_usd"] == pytest.approx(25.0)


def test_medium_signal_requires_configured_consecutive_ticks(cfg):
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.40, Regime.TREND_UP)}
    assert d.decide({}, signals, _portfolio(), {"signal_streaks": {"ETH": 1}}) == []
    assert any(x["action"] == "buy" for x in
               d.decide({}, signals, _portfolio(), {"signal_streaks": {"ETH": 2}}))


def test_dynamic_sizing_uses_small_gross_for_borderline_comeback(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_downtrend_min_momentum": 0.28,
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.30,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.30, Regime.TREND_DOWN)}
    buys = [x for x in d.decide({}, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 2}}) if x["action"] == "buy"]
    assert buys[0]["size_pct"] == pytest.approx(0.30)


def test_dynamic_sizing_uses_full_gross_for_strong_comeback(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_downtrend_min_momentum": 0.28,
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.30,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.42, Regime.TREND_DOWN)}
    buys = [x for x in d.decide({}, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 1}}) if x["action"] == "buy"]
    assert buys[0]["size_pct"] == pytest.approx(0.55)
