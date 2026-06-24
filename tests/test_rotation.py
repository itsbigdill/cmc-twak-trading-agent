"""Tests for the cross-sectional rotation decider."""

import pytest

from agent.decision import RotationDecider
from agent.signal_engine import Regime, TokenSignal


def _sig(token, score, regime):
    return TokenSignal(token=token, score=score, regime=regime, components={}, actionable=True)


def _portfolio(positions=None):
    return {"cash_usd": 1000, "total_equity_usd": 1000, "positions": positions or {}}


def _portfolio_with_marks(positions=None, values=None, avg_prices=None):
    p = _portfolio(positions)
    p["position_values"] = values or {}
    p["avg_prices"] = avg_prices or {}
    return p


def _snap(*tokens):
    return {
        t: {
            "round_trip_loss_pct": 1.2,
            "risk_level": "low",
            "history_bars": 99,
        }
        for t in tokens
    }


def test_rotation_picks_top_k_in_uptrend(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 2, "rotation_min_momentum": 0.05}}
    d = RotationDecider(cfg)
    signals = {
        "ETH": _sig("ETH", 0.8, Regime.TREND_UP),
        "CAKE": _sig("CAKE", 0.5, Regime.TREND_UP),
        "LINK": _sig("LINK", 0.1, Regime.TREND_UP),
    }
    out = d.decide(_snap("ETH", "CAKE", "LINK"), signals, _portfolio(), {})
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
    out = d.decide(_snap("ETH", "CAKE"), signals, _portfolio({"CAKE": 5.0}), {})
    actions = {(x["token"], x["action"]) for x in out}
    assert ("CAKE", "close") in actions                     # weak name exited
    assert ("ETH", "buy") in actions                        # strongest name entered


def test_rotation_downtrend_all_weak_goes_cash(cfg):
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.05, Regime.TREND_DOWN)}  # below downtrend threshold
    out = d.decide(_snap("ETH"), signals, _portfolio({"ETH": 5.0}), {})
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
    out = d.decide(_snap("ETH", "CAKE"), signals, _portfolio({"CAKE": 10.0}), {})
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
    out = d.decide(_snap("ETH", "CAKE"), signals, _portfolio({"CAKE": 5.0}), {})
    assert not any(x["token"] == "CAKE" and x["action"] == "close" for x in out)


def test_rotation_targets_sixty_percent_gross_across_two_names(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_top_k": 2,
                               "target_gross_exposure_pct": 0.60,
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.8, Regime.TREND_UP),
               "CAKE": _sig("CAKE", 0.7, Regime.TREND_UP)}
    out = d.decide(_snap("ETH", "CAKE"), signals, _portfolio(), {})
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
    buys = [x for x in d.decide(_snap("ETH", "CAKE"), signals, _portfolio(), risk)
            if x["action"] == "buy"]
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
    out = d.decide(_snap("ETH"), signals, p, risk)
    trim = next(x for x in out if x["action"] == "trim")
    assert trim["size_usd"] == pytest.approx(25.0)


def test_medium_signal_requires_configured_consecutive_ticks(cfg):
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.40, Regime.TREND_UP)}
    assert d.decide(_snap("ETH"), signals, _portfolio(), {"signal_streaks": {"ETH": 1}}) == []
    assert any(x["action"] == "buy" for x in
               d.decide(_snap("ETH"), signals, _portfolio(), {"signal_streaks": {"ETH": 2}}))


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
    buys = [x for x in d.decide(_snap("ETH"), signals, _portfolio(),
                                {"signal_streaks": {"ETH": 2}}) if x["action"] == "buy"]
    assert buys[0]["size_pct"] == pytest.approx(0.30)


def test_risk_adjusted_sizing_caps_medium_risk_recovery_scout(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_downtrend_min_momentum": 0.28,
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.20,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55,
                                                  "risk_adjusted_enabled": True,
                                                  "medium_risk_threshold": 30,
                                                  "medium_risk_gross_cap": 0.15,
                                                  "weak_cmc_threshold": 0.40,
                                                  "weak_cmc_gross_cap": 0.20}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.34, Regime.TREND_DOWN)}
    snap = _snap("ETH")
    snap["ETH"].update({
        "return_6h": 0.02,
        "return_24h": 0.04,
        "cmc_pct_1h": 0.0,
        "cmc_pct_24h": 0.05,
        "cmc_pct_7d": 0.20,
        "cmc_volume_change_24h": 0.3,
        "cmc_score": 0.20,
        "x402_token_score": 0.36,
        "token_risk_score": 45,
        "round_trip_loss_pct": 1.4,
        "distance_from_48h_high": -0.10,
    })
    buys = [x for x in d.decide(snap, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 4}})
            if x["action"] == "buy"]
    assert buys
    assert buys[0]["size_pct"] == pytest.approx(0.15)


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
    buys = [x for x in d.decide(_snap("ETH"), signals, _portfolio(),
                                {"signal_streaks": {"ETH": 1}}) if x["action"] == "buy"]
    assert buys[0]["size_pct"] == pytest.approx(0.55)


def test_fresh_downtrend_position_gets_6h_health_exit_grace(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.28,
                                             "min_quality_downtrend": 0.0,
                                             "min_return_6h_downtrend": -0.015,
                                             "min_hold_seconds_downtrend": 900,
                                             "stale_loss_pct": -0.006,
                                             "stale_loss_min_return_6h": -0.005}}}
    d = RotationDecider(cfg)
    d._now = 1_000
    signals = {"SAHARA": _sig("SAHARA", 0.34, Regime.TREND_DOWN)}
    snap = _snap("SAHARA")
    snap["SAHARA"].update({"return_6h": -0.018, "return_24h": 0.04,
                           "cmc_score": 0.2, "token_risk_score": 45})
    portfolio = _portfolio_with_marks(
        {"SAHARA": 100.0},
        values={"SAHARA": 10.0},
        avg_prices={"SAHARA": 0.10},
    )
    portfolio["position_opened_ts"] = {"SAHARA": 500.0}
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"SAHARA": 4}})
    assert not any(x["token"] == "SAHARA" and x["action"] == "close" for x in out)

    portfolio["position_opened_ts"] = {"SAHARA": 0.0}
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"SAHARA": 4}})
    assert any(x["token"] == "SAHARA" and x["action"] == "close" for x in out)


def test_high_conviction_medium_signal_gets_full_catchup_size(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_downtrend_min_momentum": 0.28,
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.30,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55,
                                                  "high_conviction_enabled": True,
                                                  "high_conviction_exposure_pct": 0.55,
                                                  "high_conviction_min_score": 0.30,
                                                  "high_conviction_min_x402": 0.25,
                                                  "high_conviction_min_cmc": 0.80,
                                                  "high_conviction_min_quality": 0.25,
                                                  "high_conviction_max_round_trip_loss_pct": 2.5,
                                                  "high_conviction_max_token_risk_score": 30,
                                                  "high_conviction_min_volume_24h_usd": 5_000_000,
                                                  "high_conviction_catchup_rank_above": 5}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.316, Regime.TREND_DOWN)}
    snap = _snap("ETH")
    snap["ETH"].update({
        "return_6h": 0.035,
        "return_24h": 0.13,
        "cmc_pct_1h": 0.009,
        "cmc_pct_24h": 0.10,
        "cmc_pct_7d": 0.33,
        "cmc_volume_24h": 65_000_000,
        "cmc_volume_change_24h": 0.2,
        "cmc_score": 1.0,
        "x402_token_score": 0.34,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.9,
        "distance_from_48h_high": -0.02,
    })
    buys = [x for x in d.decide(snap, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 2},
                                 "leaderboard_rank": 36,
                                 "leaderboard_return_pct": -7.0,
                                 "executable_return_pct": -7.0})
            if x["action"] == "buy"]
    assert buys[0]["size_pct"] == pytest.approx(0.55)
    assert "gross=0.55" in buys[0]["rationale"]


def test_high_conviction_size_still_respects_stress_drawdown_cap(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"], "rotation_downtrend_min_momentum": 0.28,
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.30,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55,
                                                  "high_conviction_enabled": True,
                                                  "high_conviction_exposure_pct": 0.55,
                                                  "high_conviction_min_score": 0.30,
                                                  "high_conviction_min_x402": 0.25,
                                                  "high_conviction_min_cmc": 0.80,
                                                  "high_conviction_min_quality": 0.25,
                                                  "high_conviction_max_round_trip_loss_pct": 2.5,
                                                  "high_conviction_max_token_risk_score": 30,
                                                  "high_conviction_min_volume_24h_usd": 5_000_000,
                                                  "stress_drawdown_pct": 0.18,
                                                  "stress_exposure_pct": 0.25}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.316, Regime.TREND_DOWN)}
    snap = _snap("ETH")
    snap["ETH"].update({
        "return_6h": 0.035,
        "return_24h": 0.13,
        "cmc_pct_1h": 0.009,
        "cmc_pct_24h": 0.10,
        "cmc_pct_7d": 0.33,
        "cmc_volume_24h": 65_000_000,
        "cmc_volume_change_24h": 0.2,
        "cmc_score": 1.0,
        "x402_token_score": 0.34,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.9,
        "distance_from_48h_high": -0.02,
    })
    buys = [x for x in d.decide(snap, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 2},
                                 "leaderboard_rank": 36,
                                 "leaderboard_return_pct": -7.0,
                                 "executable_return_pct": -7.0,
                                 "leaderboard_drawdown_pct": 18.1})
            if x["action"] == "buy"]
    assert buys[0]["size_pct"] == pytest.approx(0.25)


def test_rotation_never_buys_unvalidated_high_signal(cfg):
    d = RotationDecider(cfg)
    signals = {"SIREN": _sig("SIREN", 0.90, Regime.TREND_DOWN)}
    out = d.decide({}, signals, _portfolio(), {"signal_streaks": {"SIREN": 10}})
    assert not any(x["action"] == "buy" for x in out)


def test_held_token_decays_below_floor_to_cash(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.24}}}
    d = RotationDecider(cfg)
    signals = {"SIREN": _sig("SIREN", 0.20, Regime.TREND_DOWN)}
    out = d.decide({}, signals, _portfolio({"SIREN": 100.0}), {})
    assert any(x["token"] == "SIREN" and x["action"] == "close" for x in out)


def test_downtrend_entry_rejects_late_hot_candle_even_with_good_signal(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "entry_filter": {"enabled": True,
                                                "min_quality_downtrend": -1.0,
                                                "max_cmc_pct_1h_downtrend": 0.03,
                                                "max_cmc_pct_24h_downtrend": 0.18,
                                                "max_cmc_pct_7d_downtrend": 0.45,
                                                "min_return_6h_downtrend": 0.0,
                                                "max_return_6h_downtrend": 0.08}}}
    d = RotationDecider(cfg)
    signals = {"LAB": _sig("LAB", 0.42, Regime.TREND_DOWN)}
    snap = _snap("LAB")
    snap["LAB"].update({
        "return_6h": 0.025,
        "return_24h": 0.078,
        "cmc_pct_1h": 0.039,   # looks like a late candle; do not chase
        "cmc_pct_24h": 0.067,
        "cmc_pct_7d": 0.30,
        "distance_from_48h_high": -0.02,
    })
    out = d.decide(snap, signals, _portfolio(), {"signal_streaks": {"LAB": 2}})
    assert not any(x["token"] == "LAB" and x["action"] == "buy" for x in out)


def test_downtrend_entry_rejects_hot_move_without_volume_confirmation(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "entry_filter": {"enabled": True,
                                                "min_quality_downtrend": -1.0,
                                                "max_cmc_pct_1h_downtrend": 0.03,
                                                "max_cmc_pct_24h_downtrend": 0.18,
                                                "max_cmc_pct_7d_downtrend": 0.45,
                                                "min_return_6h_downtrend": 0.0,
                                                "max_return_6h_downtrend": 0.08,
                                                "min_volume_change_24h_downtrend": -0.50,
                                                "hot_volume_min_change_24h_downtrend": -0.10}}}
    d = RotationDecider(cfg)
    signals = {"LAB": _sig("LAB", 0.43, Regime.TREND_DOWN)}
    snap = _snap("LAB")
    snap["LAB"].update({
        "return_6h": 0.035,
        "return_24h": 0.06,
        "cmc_pct_1h": 0.025,
        "cmc_pct_24h": 0.15,
        "cmc_pct_7d": 0.20,
        "cmc_volume_change_24h": -0.25,
        "distance_from_48h_high": -0.02,
    })
    out = d.decide(snap, signals, _portfolio(), {"signal_streaks": {"LAB": 2}})
    assert not any(x["token"] == "LAB" and x["action"] == "buy" for x in out)


def test_downtrend_pullback_exception_allows_cost_controlled_reentry(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_min_momentum": 0.28,
                               "entry_filter": {"enabled": True,
                                                "min_quality_downtrend": -1.0,
                                                "max_cmc_pct_1h_downtrend": 0.03,
                                                "max_cmc_pct_24h_downtrend": 0.18,
                                                "max_cmc_pct_7d_downtrend": 0.45,
                                                "min_return_6h_downtrend": 0.0,
                                                "max_return_6h_downtrend": 0.08,
                                                "pullback_exception_enabled": True,
                                                "pullback_exposure_pct": 0.40,
                                                "pullback_min_score": 0.32,
                                                "pullback_min_quality_downtrend": 0.20,
                                                "pullback_min_cmc_pct_1h_downtrend": -0.12,
                                                "pullback_max_cmc_pct_1h_downtrend": -0.03,
                                                "pullback_min_return_6h_downtrend": -0.02,
                                                "pullback_max_return_6h_downtrend": 0.04,
                                                "pullback_max_cmc_pct_24h_downtrend": 0.24,
                                                "pullback_max_cmc_pct_7d_downtrend": 0.55,
                                                "pullback_min_x402": 0.25,
                                                "pullback_min_cmc": 0.80,
                                                "pullback_max_round_trip_loss_pct": 2.0,
                                                "pullback_max_token_risk_score": 30},
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.30,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55,
                                                  "high_conviction_enabled": True,
                                                  "high_conviction_exposure_pct": 0.55,
                                                  "high_conviction_min_score": 0.30,
                                                  "high_conviction_min_x402": 0.25,
                                                  "high_conviction_min_cmc": 0.80,
                                                  "high_conviction_min_quality": 0.25,
                                                  "high_conviction_max_round_trip_loss_pct": 2.5,
                                                  "high_conviction_max_token_risk_score": 30,
                                                  "high_conviction_min_volume_24h_usd": 5_000_000}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.33, Regime.TREND_DOWN)}
    snap = _snap("ETH")
    snap["ETH"].update({
        "return_6h": 0.006,
        "return_24h": 0.20,
        "cmc_pct_1h": -0.06,
        "cmc_pct_24h": 0.21,       # above normal 18% cap, allowed only by pullback exception
        "cmc_pct_7d": 0.38,
        "cmc_volume_24h": 80_000_000,
        "cmc_volume_change_24h": 1.0,
        "cmc_score": 0.90,
        "x402_token_score": 0.26,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.9,
        "distance_from_48h_high": -0.06,
    })
    buys = [x for x in d.decide(snap, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 4},
                                 "leaderboard_rank": 40,
                                 "leaderboard_return_pct": -7.0,
                                 "executable_return_pct": -7.0})
            if x["action"] == "buy"]
    assert buys
    assert buys[0]["size_pct"] == pytest.approx(0.40)
    assert "validated_pullback" in buys[0]["rationale"]


def test_downtrend_pullback_exception_rejects_expensive_round_trip(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_min_momentum": 0.28,
                               "entry_filter": {"enabled": True,
                                                "min_quality_downtrend": -1.0,
                                                "max_cmc_pct_24h_downtrend": 0.18,
                                                "pullback_exception_enabled": True,
                                                "pullback_min_score": 0.32,
                                                "pullback_min_quality_downtrend": 0.20,
                                                "pullback_min_cmc_pct_1h_downtrend": -0.12,
                                                "pullback_max_cmc_pct_1h_downtrend": -0.03,
                                                "pullback_min_return_6h_downtrend": -0.02,
                                                "pullback_max_return_6h_downtrend": 0.04,
                                                "pullback_max_cmc_pct_24h_downtrend": 0.24,
                                                "pullback_max_cmc_pct_7d_downtrend": 0.55,
                                                "pullback_min_x402": 0.25,
                                                "pullback_min_cmc": 0.80,
                                                "pullback_max_round_trip_loss_pct": 2.0,
                                                "pullback_max_token_risk_score": 30}}}
    d = RotationDecider(cfg)
    signals = {"ETH": _sig("ETH", 0.36, Regime.TREND_DOWN)}
    snap = _snap("ETH")
    snap["ETH"].update({
        "return_6h": 0.006,
        "return_24h": 0.20,
        "cmc_pct_1h": -0.06,
        "cmc_pct_24h": 0.21,
        "cmc_pct_7d": 0.38,
        "cmc_volume_change_24h": 1.0,
        "cmc_score": 0.90,
        "x402_token_score": 0.26,
        "token_risk_score": 10,
        "round_trip_loss_pct": 2.6,
    })
    out = d.decide(snap, signals, _portfolio(),
                   {"signal_streaks": {"ETH": 4}})
    assert not any(x["token"] == "ETH" and x["action"] == "buy" for x in out)


def test_held_token_exits_when_short_momentum_breaks_in_downtrend(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.28,
                                             "min_quality_downtrend": -1.0,
                                             "min_return_6h_downtrend": -0.015}}}
    d = RotationDecider(cfg)
    signals = {"SIREN": _sig("SIREN", 0.35, Regime.TREND_DOWN)}
    snap = _snap("SIREN")
    snap["SIREN"].update({"return_6h": -0.04, "return_24h": 0.0})
    out = d.decide(snap, signals, _portfolio({"SIREN": 100.0}), {})
    close = next(x for x in out if x["token"] == "SIREN" and x["action"] == "close")
    assert "health exit" in close["rationale"]
    assert "rotate out" not in close["rationale"]


def test_held_token_micro_profit_take_trims_winner(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.20,
                                             "micro_profit_take_pct": 0.015,
                                             "micro_profit_sell_fraction": 0.45,
                                             "min_micro_profit_sell_usd": 1.0},
                               "rotation_downtrend_min_momentum": 0.28,
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    signals = {"SAHARA": _sig("SAHARA", 0.34, Regime.TREND_DOWN)}
    snap = _snap("SAHARA")
    snap["SAHARA"].update({"return_6h": 0.02, "return_24h": 0.03})
    portfolio = _portfolio_with_marks(
        {"SAHARA": 100.0},
        values={"SAHARA": 10.20},
        avg_prices={"SAHARA": 0.10},
    )
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"SAHARA": 2}})
    trim = next(x for x in out if x["token"] == "SAHARA" and x["action"] == "trim")
    assert trim["size_usd"] == pytest.approx(4.59)
    assert "micro profit take" in trim["rationale"]
