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


def _portfolio_with_timing(positions=None, values=None, avg_prices=None,
                           opened_ts=None, rotation_exited_at=None):
    p = _portfolio_with_marks(positions, values, avg_prices)
    p["position_opened_ts"] = opened_ts or {}
    p["rotation_exited_at"] = rotation_exited_at or {}
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


def test_held_target_underweight_gets_topped_up(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 1,
                               "rotation_downtrend_min_momentum": 0.28,
                               "target_gross_exposure_pct": 0.55,
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.20,
                                             "min_quality_downtrend": -1.0,
                                             "min_return_6h_downtrend": -0.20},
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    signals = {"LAB": _sig("LAB", 0.41, Regime.TREND_DOWN)}
    snap = _snap("LAB")
    snap["LAB"].update({"return_6h": 0.01, "return_24h": 0.12})
    portfolio = _portfolio_with_marks(
        {"LAB": 0.33},
        values={"LAB": 5.3},
        avg_prices={"LAB": 16.2},
    )
    portfolio["cash_usd"] = 13.9
    portfolio["total_equity_usd"] = 19.2
    buys = [x for x in d.decide(snap, signals, portfolio,
                                {"signal_streaks": {"LAB": 3}})
            if x["token"] == "LAB" and x["action"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["size_pct"] == pytest.approx((19.2 * 0.55 - 5.3) / 13.9, abs=1e-4)


def test_tiny_held_target_top_up_is_suppressed(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 2,
                               "rotation_downtrend_min_momentum": 0.28,
                               "target_gross_exposure_pct": 0.55,
                               "min_rebalance_usd": 1.0,
                               "entry_filter": {"enabled": False},
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.20,
                                             "min_quality_downtrend": -1.0,
                                             "min_return_6h_downtrend": -0.20},
                               "dynamic_sizing": {"enabled": False}},
           "twak": {**cfg["twak"], "min_swap_quote": 0.25}}
    d = RotationDecider(cfg)
    signals = {"LAB": _sig("LAB", 0.41, Regime.TREND_DOWN),
               "ETH": _sig("ETH", 0.34, Regime.TREND_DOWN)}
    snap = _snap("LAB", "ETH")
    snap["LAB"].update({"return_6h": 0.01, "return_24h": 0.12})
    snap["ETH"].update({"return_6h": 0.02, "return_24h": 0.04})
    portfolio = _portfolio_with_marks(
        {"LAB": 0.33},
        values={"LAB": 5.27},
        avg_prices={"LAB": 16.2},
    )
    portfolio["cash_usd"] = 13.9
    portfolio["total_equity_usd"] = 19.2
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"LAB": 3, "ETH": 3}})
    assert not any(x["token"] == "LAB" and x["action"] == "buy" for x in out)


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


def test_high_conviction_reentry_bypasses_cooldown_in_catchup(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 1,
                               "rotation_downtrend_min_momentum": 0.28,
                               "rotation_reentry_cooldown_hours": 4,
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
    d._now = 10_000
    signals = {"ETH": _sig("ETH", 0.41, Regime.TREND_DOWN)}
    snap = _snap("ETH")
    snap["ETH"].update({
        "return_6h": 0.025,
        "return_24h": 0.11,
        "cmc_pct_1h": 0.008,
        "cmc_pct_24h": 0.10,
        "cmc_pct_7d": 0.24,
        "cmc_volume_24h": 80_000_000,
        "cmc_volume_change_24h": 0.25,
        "cmc_score": 1.0,
        "x402_token_score": 0.39,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.9,
        "distance_from_48h_high": -0.03,
    })
    portfolio = _portfolio_with_timing({}, rotation_exited_at={"ETH": 10_000 - 600})
    out = d.decide(
        snap,
        signals,
        portfolio,
        {
            "signal_streaks": {"ETH": 2},
            "leaderboard_rank": 20,
            "leaderboard_return_pct": -8.7,
            "executable_return_pct": -8.7,
        },
    )
    assert any(x["token"] == "ETH" and x["action"] == "buy" for x in out)
    assert "reentry_cooldown_bypassed_high_conviction" in d.last_debug["anti_churn"]["ETH"]


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


def test_downtrend_scout_exception_allows_small_recovery_probe(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_min_momentum": 0.28,
                               "entry_filter": {"enabled": True,
                                                "min_quality_downtrend": -1.0,
                                                "min_return_6h_downtrend": 0.0,
                                                "max_return_6h_downtrend": 0.08,
                                                "scout_exception_enabled": True,
                                                "scout_exposure_pct": 0.18,
                                                "scout_min_score": 0.31,
                                                "scout_min_quality_downtrend": 0.30,
                                                "scout_min_return_6h_downtrend": -0.02,
                                                "scout_max_return_6h_downtrend": 0.02,
                                                "scout_min_return_24h_downtrend": 0.02,
                                                "scout_max_cmc_pct_24h_downtrend": 0.12,
                                                "scout_max_cmc_pct_7d_downtrend": 0.30,
                                                "scout_min_x402": 0.25,
                                                "scout_min_cmc": 0.25,
                                                "scout_max_round_trip_loss_pct": 1.8,
                                                "scout_max_token_risk_score": 30,
                                                "scout_min_volume_24h_usd": 5_000_000},
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
                                                  "weak_cmc_gross_cap": 0.30}}}
    d = RotationDecider(cfg)
    signals = {
        "ETH": _sig("ETH", 0.335, Regime.TREND_DOWN),
        "LINK": _sig("LINK", 0.290, Regime.TREND_DOWN),
        "DOGE": _sig("DOGE", 0.280, Regime.TREND_DOWN),
    }
    snap = _snap("ETH", "LINK", "DOGE")
    snap["ETH"].update({
        "return_6h": -0.012,
        "return_24h": 0.05,
        "cmc_pct_1h": -0.01,
        "cmc_pct_24h": 0.05,
        "cmc_pct_7d": -0.10,
        "cmc_volume_24h": 20_000_000,
        "cmc_volume_change_24h": 0.20,
        "cmc_score": 0.35,
        "x402_token_score": 0.27,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.4,
        "distance_from_48h_high": -0.04,
    })
    for token in ("LINK", "DOGE"):
        snap[token].update({
            "return_6h": -0.05,
            "return_24h": -0.10,
            "cmc_pct_24h": -0.05,
            "cmc_pct_7d": -0.20,
            "cmc_volume_24h": 10_000_000,
            "cmc_score": 0.10,
            "x402_token_score": 0.10,
            "token_risk_score": 10,
            "round_trip_loss_pct": 1.4,
        })
    buys = [x for x in d.decide(snap, signals, _portfolio(),
                                {"signal_streaks": {"ETH": 3, "LINK": 3, "DOGE": 3},
                                 "leaderboard_rank": 31,
                                 "leaderboard_return_pct": -9.0,
                                 "executable_return_pct": -9.0})
            if x["action"] == "buy"]
    assert buys
    assert buys[0]["size_pct"] == pytest.approx(0.18)
    assert "validated_scout" in buys[0]["rationale"]
    assert "gross=0.18" in buys[0]["rationale"]


def test_liquid_confirmed_continuation_ignores_weak_volume_change_noise(cfg):
    cfg = {**cfg,
           "twak": {**cfg["twak"],
                    "token_contracts": {"BEAT": "0x1", "XPL": "0x2"},
                    "deny_buy": [], "sell_only_tokens": []},
           "decision": {**cfg["decision"],
                        "rotation_downtrend_topk": 1,
                        "rotation_downtrend_min_momentum": 0.275,
                        "target_gross_exposure_pct": 0.55,
                        "entry_filter": {**cfg["decision"]["entry_filter"],
                                         "enabled": True,
                                         "liquid_continuation_exception_enabled": True,
                                         "liquid_continuation_min_x402": 0.25,
                                         "liquid_continuation_min_cmc": 0.20,
                                         "liquid_continuation_max_round_trip_loss_pct": 1.5,
                                         "liquid_continuation_min_volume_24h_usd": 10_000_000},
                        "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    signals = {
        "XPL": _sig("XPL", 0.264, Regime.TREND_DOWN),
        "BEAT": _sig("BEAT", 0.342, Regime.TREND_DOWN),
    }
    snap = _snap("BEAT", "XPL")
    snap["BEAT"].update({
        "return_6h": 0.031,
        "return_24h": 0.109,
        "cmc_pct_1h": -0.001,
        "cmc_pct_24h": 0.066,
        "cmc_pct_7d": 0.170,
        "cmc_volume_change_24h": -0.461,
        "cmc_volume_24h": 59_000_000,
        "x402_token_score": 0.304,
        "cmc_score": 0.240,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.197,
        "distance_from_48h_high": -0.10,
    })
    snap["XPL"].update({
        "return_6h": 0.034,
        "return_24h": 0.100,
        "cmc_pct_1h": -0.008,
        "cmc_pct_24h": 0.095,
        "cmc_pct_7d": -0.058,
        "cmc_volume_change_24h": 0.10,
        "cmc_volume_24h": 20_000_000,
        "x402_token_score": 0.225,
        "cmc_score": 0.542,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.363,
        "distance_from_48h_high": -0.10,
    })

    out = d.decide(snap, signals, _portfolio(), {"signal_streaks": {"BEAT": 4, "XPL": 4}})
    buys = [x for x in out if x["action"] == "buy"]

    assert [x["token"] for x in buys] == ["BEAT"]
    assert "liquid_confirmed_continuation" in buys[0]["rationale"]


def test_surviving_scout_scales_up_while_far_behind(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 1,
                               "rotation_downtrend_min_momentum": 0.28,
                               "target_gross_exposure_pct": 0.55,
                               "entry_filter": {"enabled": True,
                                                "scout_exception_enabled": True,
                                                "scout_exposure_pct": 0.18,
                                                "scout_min_score": 0.31,
                                                "scout_min_quality_downtrend": 0.30,
                                                "scout_min_return_6h_downtrend": -0.02,
                                                "scout_max_return_6h_downtrend": 0.02,
                                                "scout_min_return_24h_downtrend": 0.02,
                                                "scout_max_cmc_pct_24h_downtrend": 0.12,
                                                "scout_max_cmc_pct_7d_downtrend": 0.30,
                                                "scout_min_x402": 0.25,
                                                "scout_min_cmc": 0.25,
                                                "scout_max_round_trip_loss_pct": 1.8,
                                                "scout_max_token_risk_score": 30,
                                                "scout_min_volume_24h_usd": 5_000_000},
                               "recovery_escalation": {"enabled": True,
                                                       "rank_above": 20,
                                                       "min_gap_to_top5_pct": 5.0,
                                                       "min_hold_seconds": 600,
                                                       "scale_gross_exposure_pct": 0.36,
                                                       "confirmed_hold_seconds": 1800,
                                                       "confirmed_gross_exposure_pct": 0.50,
                                                       "max_leaderboard_drawdown_pct": 24.0},
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.20,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55,
                                                  "risk_adjusted_enabled": True,
                                                  "weak_cmc_threshold": 0.40,
                                                  "weak_cmc_gross_cap": 0.30}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    signals = {"XPL": _sig("XPL", 0.290, Regime.TREND_DOWN)}
    snap = _snap("XPL")
    snap["XPL"].update({
        "return_6h": -0.010,
        "return_24h": 0.055,
        "cmc_pct_24h": 0.055,
        "cmc_pct_7d": 0.10,
        "cmc_volume_24h": 20_000_000,
        "cmc_volume_change_24h": 1.5,
        "vol_adjusted_return": 3.0,
        "cmc_score": -0.01,
        "x402_token_score": 0.35,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.4,
    })
    portfolio = _portfolio_with_timing(
        {"XPL": 20.0},
        values={"XPL": 3.60},
        avg_prices={"XPL": 0.18},
        opened_ts={"XPL": 10_000 - 700},
    )
    portfolio["total_equity_usd"] = 20.0
    portfolio["cash_usd"] = 16.4
    out = d.decide(snap, signals, portfolio,
                   {"signal_streaks": {"XPL": 3},
                    "leaderboard_rank": 33,
                    "leaderboard_return_pct": -9.4,
                    "leaderboard_top5_return_pct": 0.24,
                    "leaderboard_drawdown_pct": 17.3,
                    "executable_return_pct": -9.4})
    buy = next(x for x in out if x["token"] == "XPL" and x["action"] == "buy")
    assert buy["size_pct"] == pytest.approx((20.0 * 0.36 - 3.60) / 16.4, rel=1e-3)
    assert "gross=0.36" in buy["rationale"]


def test_fresh_scout_does_not_scale_before_min_hold(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 1,
                               "rotation_downtrend_min_momentum": 0.28,
                               "entry_filter": {"enabled": True,
                                                "scout_exception_enabled": True,
                                                "scout_exposure_pct": 0.18,
                                                "scout_min_score": 0.31,
                                                "scout_min_quality_downtrend": 0.30,
                                                "scout_min_return_6h_downtrend": -0.02,
                                                "scout_max_return_6h_downtrend": 0.02,
                                                "scout_min_return_24h_downtrend": 0.02,
                                                "scout_max_cmc_pct_24h_downtrend": 0.12,
                                                "scout_max_cmc_pct_7d_downtrend": 0.30,
                                                "scout_min_x402": 0.25,
                                                "scout_min_cmc": 0.25,
                                                "scout_max_round_trip_loss_pct": 1.8,
                                                "scout_max_token_risk_score": 30,
                                                "scout_min_volume_24h_usd": 5_000_000},
                               "recovery_escalation": {"enabled": True,
                                                       "min_hold_seconds": 600,
                                                       "scale_gross_exposure_pct": 0.36},
                               "dynamic_sizing": {"enabled": True,
                                                  "low_score": 0.28,
                                                  "mid_score": 0.32,
                                                  "high_score": 0.38,
                                                  "low_exposure_pct": 0.20,
                                                  "mid_exposure_pct": 0.40,
                                                  "high_exposure_pct": 0.55}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    signals = {"XPL": _sig("XPL", 0.335, Regime.TREND_DOWN)}
    snap = _snap("XPL")
    snap["XPL"].update({
        "return_6h": -0.010,
        "return_24h": 0.055,
        "cmc_pct_24h": 0.055,
        "cmc_pct_7d": 0.10,
        "cmc_volume_24h": 20_000_000,
        "cmc_volume_change_24h": 1.5,
        "vol_adjusted_return": 3.0,
        "cmc_score": 1.0,
        "x402_token_score": 0.35,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.4,
    })
    portfolio = _portfolio_with_timing(
        {"XPL": 20.0},
        values={"XPL": 3.60},
        avg_prices={"XPL": 0.18},
        opened_ts={"XPL": 10_000 - 300},
    )
    portfolio["total_equity_usd"] = 20.0
    portfolio["cash_usd"] = 16.4
    out = d.decide(snap, signals, portfolio,
                   {"signal_streaks": {"XPL": 3},
                    "leaderboard_rank": 33,
                    "leaderboard_return_pct": -9.4,
                    "leaderboard_top5_return_pct": 0.24,
                    "leaderboard_drawdown_pct": 17.3,
                    "executable_return_pct": -9.4})
    assert not any(x["token"] == "XPL" and x["action"] == "buy" for x in out)
    assert d.last_debug["gross"] == pytest.approx(0.18)


def test_downtrend_scout_exception_rejects_weak_cmc_or_bad_route(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_min_momentum": 0.28,
                               "entry_filter": {"enabled": True,
                                                "min_quality_downtrend": -1.0,
                                                "min_return_6h_downtrend": 0.0,
                                                "max_return_6h_downtrend": 0.08,
                                                "scout_exception_enabled": True,
                                                "scout_min_score": 0.31,
                                                "scout_min_quality_downtrend": 0.30,
                                                "scout_min_return_6h_downtrend": -0.02,
                                                "scout_max_return_6h_downtrend": 0.02,
                                                "scout_min_return_24h_downtrend": 0.02,
                                                "scout_max_cmc_pct_24h_downtrend": 0.12,
                                                "scout_max_cmc_pct_7d_downtrend": 0.30,
                                                "scout_min_x402": 0.25,
                                                "scout_min_cmc": 0.25,
                                                "scout_max_round_trip_loss_pct": 1.8,
                                                "scout_max_token_risk_score": 30,
                                                "scout_min_volume_24h_usd": 5_000_000}}}
    d = RotationDecider(cfg)
    signals = {
        "ETH": _sig("ETH", 0.335, Regime.TREND_DOWN),
        "LINK": _sig("LINK", 0.290, Regime.TREND_DOWN),
        "DOGE": _sig("DOGE", 0.280, Regime.TREND_DOWN),
    }
    snap = _snap("ETH", "LINK", "DOGE")
    snap["ETH"].update({
        "return_6h": -0.012,
        "return_24h": 0.05,
        "cmc_pct_24h": 0.05,
        "cmc_pct_7d": -0.10,
        "cmc_volume_24h": 20_000_000,
        "cmc_score": -0.10,
        "x402_token_score": 0.27,
        "token_risk_score": 10,
        "round_trip_loss_pct": 1.4,
    })
    for token in ("LINK", "DOGE"):
        snap[token].update({
            "return_6h": -0.05,
            "return_24h": -0.10,
            "cmc_pct_24h": -0.05,
            "cmc_pct_7d": -0.20,
            "cmc_volume_24h": 10_000_000,
            "cmc_score": 0.10,
            "x402_token_score": 0.10,
            "token_risk_score": 10,
            "round_trip_loss_pct": 1.4,
        })
    out = d.decide(snap, signals, _portfolio(),
                   {"signal_streaks": {"ETH": 3, "LINK": 3, "DOGE": 3},
                    "leaderboard_rank": 31,
                    "leaderboard_return_pct": -9.0,
                    "executable_return_pct": -9.0})
    assert not any(x["token"] == "ETH" and x["action"] == "buy" for x in out)
    assert d.last_debug["rejects"]["ETH"].startswith("EntryGate:bad_6h")

    snap["ETH"]["cmc_score"] = 0.35
    snap["ETH"]["round_trip_loss_pct"] = 2.4
    out = d.decide(snap, signals, _portfolio(),
                   {"signal_streaks": {"ETH": 3, "LINK": 3, "DOGE": 3},
                    "leaderboard_rank": 31,
                    "leaderboard_return_pct": -9.0,
                    "executable_return_pct": -9.0})
    assert not any(x["token"] == "ETH" and x["action"] == "buy" for x in out)
    assert d.last_debug["rejects"]["ETH"].startswith("EntryGate:bad_6h")


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


def test_fresh_held_token_quality_exit_respects_min_hold(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.20,
                                             "min_quality_downtrend": 0.99,
                                             "min_return_6h_downtrend": -0.20,
                                             "min_hold_seconds_downtrend": 2700},
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    signals = {"LAB": _sig("LAB", 0.35, Regime.TREND_DOWN)}
    snap = _snap("LAB")
    snap["LAB"].update({"return_6h": 0.0, "return_24h": 0.02})

    fresh = _portfolio_with_timing(
        {"LAB": 1.0},
        values={"LAB": 10.0},
        avg_prices={"LAB": 10.0},
        opened_ts={"LAB": 9_400},
    )
    out = d.decide(snap, signals, fresh, {"signal_streaks": {"LAB": 3}})
    assert not any(x["token"] == "LAB" and x["action"] == "close" for x in out)

    stale = _portfolio_with_timing(
        {"LAB": 1.0},
        values={"LAB": 10.0},
        avg_prices={"LAB": 10.0},
        opened_ts={"LAB": 7_000},
    )
    out = d.decide(snap, signals, stale, {"signal_streaks": {"LAB": 3}})
    close = next(x for x in out if x["token"] == "LAB" and x["action"] == "close")
    assert "quality" in close["rationale"]


def test_fresh_held_token_small_score_wiggle_respects_min_hold(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.28,
                                             "floor_score_buffer_downtrend": 0.02,
                                             "fresh_hard_floor_score_downtrend": 0.18,
                                             "min_quality_downtrend": -1.0,
                                             "min_return_6h_downtrend": -0.20,
                                             "min_hold_seconds_downtrend": 2700},
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    snap = _snap("UAI")
    snap["UAI"].update({"return_6h": 0.0, "return_24h": 0.03})
    fresh = _portfolio_with_timing(
        {"UAI": 1.0},
        values={"UAI": 10.0},
        avg_prices={"UAI": 10.0},
        opened_ts={"UAI": 9_700},
    )

    signals = {"UAI": _sig("UAI", 0.252, Regime.TREND_DOWN)}
    out = d.decide(snap, signals, fresh, {"signal_streaks": {"UAI": 3}})
    assert not any(x["token"] == "UAI" and x["action"] == "close" for x in out)

    signals = {"UAI": _sig("UAI", 0.12, Regime.TREND_DOWN)}
    out = d.decide(snap, signals, fresh, {"signal_streaks": {"UAI": 3}})
    close = next(x for x in out if x["token"] == "UAI" and x["action"] == "close")
    assert "score" in close["rationale"]


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


def test_fresh_recovery_position_not_rebalance_trimmed(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 2,
                               "target_gross_exposure_pct": 0.55,
                               "min_rebalance_usd": 1.0,
                               "min_rebalance_hold_seconds": 2700,
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.20,
                                             "min_quality_downtrend": -1.0,
                                             "min_return_6h_downtrend": -0.20},
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    signals = {"LAB": _sig("LAB", 0.41, Regime.TREND_DOWN),
               "APR": _sig("APR", 0.34, Regime.TREND_DOWN)}
    snap = _snap("LAB", "APR")
    snap["LAB"].update({"return_6h": 0.01, "return_24h": 0.12})
    snap["APR"].update({"return_6h": 0.02, "return_24h": 0.04})
    portfolio = _portfolio_with_timing(
        {"LAB": 0.475},
        values={"LAB": 7.7},
        avg_prices={"LAB": 16.2},
        opened_ts={"LAB": 10_000 - 300},
    )
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"LAB": 3, "APR": 3}})
    assert not any(x["token"] == "LAB" and x["action"] == "trim" for x in out)


def test_fresh_losing_recovery_position_requires_bigger_rotation_edge(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 1,
                               "rotation_downtrend_min_momentum": 0.28,
                               "rotation_score_hurdle": 0.18,
                               "held_exit": {"enabled": True,
                                             "floor_score_downtrend": 0.20,
                                             "min_quality_downtrend": -1.0,
                                             "min_return_6h_downtrend": -0.20,
                                             "fresh_loss_rotation_min_hold_seconds_downtrend": 2700,
                                             "fresh_loss_rotation_hurdle_downtrend": 1.0,
                                             "fresh_loss_rotation_max_pnl_pct": 0.003},
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    signals = {"ETH": _sig("ETH", 0.55, Regime.TREND_DOWN),
               "SAHARA": _sig("SAHARA", 0.30, Regime.TREND_DOWN)}
    snap = _snap("ETH", "SAHARA")
    snap["ETH"].update({"return_6h": 0.03, "return_24h": 0.05})
    snap["SAHARA"].update({"return_6h": 0.01, "return_24h": 0.03})
    portfolio = _portfolio_with_timing(
        {"SAHARA": 100.0},
        values={"SAHARA": 9.95},
        avg_prices={"SAHARA": 0.10},
        opened_ts={"SAHARA": 10_000 - 600},
    )
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"ETH": 3, "SAHARA": 3}})
    assert not any(x["token"] == "SAHARA" and x["action"] == "close" for x in out)
    assert not any(x["token"] == "ETH" and x["action"] == "buy" for x in out)


def test_rotation_reentry_cooldown_survives_restart_via_portfolio_state(cfg):
    cfg = {**cfg, "decision": {**cfg["decision"],
                               "rotation_downtrend_topk": 1,
                               "rotation_downtrend_min_momentum": 0.28,
                               "rotation_reentry_cooldown_hours": 4,
                               "entry_filter": {**cfg["decision"].get("entry_filter", {}),
                                                "min_quality_downtrend": -1.0},
                               "dynamic_sizing": {"enabled": False}}}
    d = RotationDecider(cfg)
    d._now = 10_000
    signals = {"SAHARA": _sig("SAHARA", 0.60, Regime.TREND_DOWN)}
    snap = _snap("SAHARA")
    snap["SAHARA"].update({"return_6h": 0.02, "return_24h": 0.05})
    portfolio = _portfolio_with_timing(
        {},
        rotation_exited_at={"SAHARA": 10_000 - 600},
    )
    out = d.decide(snap, signals, portfolio, {"signal_streaks": {"SAHARA": 3}})
    assert not any(x["token"] == "SAHARA" and x["action"] == "buy" for x in out)
