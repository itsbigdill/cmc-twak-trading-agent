from agent.agent import _decision_trace
from agent.signal_engine import Regime, TokenSignal


def _sig(token, score, regime=Regime.TREND_DOWN):
    return TokenSignal(
        token=token,
        score=score,
        regime=regime,
        components={"cmc": 0.1, "x402": 0.2},
        actionable=False,
    )


def _cfg(cfg):
    cfg = {
        **cfg,
        "decision": {
            **cfg["decision"],
            "strategy_label": "test",
            "rotation_downtrend_min_momentum": 0.38,
            "signal_confirmation": {"immediate_score": 0.48, "required_ticks": 3},
        },
        "execution": {**cfg["execution"], "max_round_trip_loss_pct": 3.0},
        "twak": {**cfg["twak"], "token_contracts": {"COAI": "0xcoai", "BAS": "0xbas"}},
        "universe_runtime": {},
    }
    return cfg


def test_trace_says_no_runtime_validated_candidate(cfg):
    cfg = _cfg(cfg)
    signals = {"COAI": _sig("COAI", 0.42)}
    trace = _decision_trace(
        cfg, "tick", {"COAI": {"round_trip_loss_pct": 999.0}},
        signals, {"cash_usd": 10, "total_equity_usd": 10, "positions": {}},
        {"signal_streaks": {"COAI": 3}}, [], {"COAI"},
    )
    assert trace["kind"] == "decision_trace"
    assert trace["reason"] == "no_runtime_validated_candidate"
    assert trace["best_tradeable"]["token"] == "COAI"
    assert trace["best_validated"] is None


def test_trace_says_best_validated_below_downtrend_gate(cfg):
    cfg = _cfg(cfg)
    snap = {"COAI": {"round_trip_loss_pct": 1.2, "risk_level": "low", "history_bars": 42}}
    signals = {"COAI": _sig("COAI", 0.31)}
    trace = _decision_trace(
        cfg, "tick", snap, signals,
        {"cash_usd": 10, "total_equity_usd": 10, "positions": {}},
        {"signal_streaks": {"COAI": 3}}, [], {"COAI"},
    )
    assert trace["reason"] == "best_validated_score_below_rotation_downtrend_min_momentum"
    assert trace["gate"]["required"] == 0.38
    assert trace["gate"]["actual"] == 0.31
    assert trace["best_validated"]["round_trip_loss_pct"] == 1.2


def test_trace_records_candidate_decisions_and_risk_outcomes(cfg):
    cfg = _cfg(cfg)
    decision = {"token": "COAI", "action": "buy", "size_pct": 0.25,
                "confidence": 0.7, "rationale": "test"}
    outcome = {"token": "COAI", "action": "buy", "approved": True,
               "reason": "approved", "adjusted_size_usd": 2.5}
    snap = {"COAI": {"round_trip_loss_pct": 1.2, "risk_level": "low", "history_bars": 42}}
    signals = {"COAI": _sig("COAI", 0.5)}
    trace = _decision_trace(
        cfg, "tick", snap, signals,
        {"cash_usd": 10, "total_equity_usd": 10, "positions": {}},
        {"signal_streaks": {"COAI": 1}}, [decision], {"COAI"},
        risk_outcomes=[outcome],
    )
    assert trace["reason"] == "candidate_decisions_emitted"
    assert trace["final_action"] == "buy:COAI"
    assert trace["candidate_decisions"] == [decision]
    assert trace["risk_outcomes"] == [outcome]
