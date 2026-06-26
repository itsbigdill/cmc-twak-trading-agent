import copy

from agent.agent import process_tick
from agent.executor import MockExecutor
from agent.logbook import DecisionLog
from agent.profit_lock import progressive_profit_lock
from agent.state import PortfolioState, Position


class HoldDecider:
    def decide(self, snapshot, signals, portfolio, risk_limits):
        return []


def test_trailing_profit_lock_closes_after_reversal(cfg, tmp_path):
    cfg = copy.deepcopy(cfg)
    cfg["paths"]["state_file"] = str(tmp_path / "state.json")
    cfg["paths"]["decision_log"] = str(tmp_path / "decisions.jsonl")
    state = PortfolioState(cash_usd=90.0, initial_equity=100.0, peak_equity=101.5)
    state.positions["RAY"] = Position(
        token="RAY", qty=10.0, avg_price=1.0, peak_executable_price=1.30
    )
    snapshot = {"RAY": {"price": 1.15, "executable_price": 1.15}}

    process_tick(
        cfg, state, snapshot, {"RAY": 1.15}, HoldDecider(), MockExecutor(cfg),
        DecisionLog(cfg["paths"]["decision_log"]), now_ts=10_000,
        date_str="2026-06-23", hour=1, ts_iso="profit-lock-test",
    )

    assert "RAY" not in state.positions
    assert state.trade_count_total == 1


def test_progressive_profit_lock_defends_four_percent_peak():
    lock = {
        "enabled": True,
        "activation_pct": 0.025,
        "breakeven_floor_pct": 0.012,
        "floor_steps": [
            {"peak_pct": 0.025, "floor_pct": 0.012},
            {"peak_pct": 0.040, "floor_pct": 0.030},
            {"peak_pct": 0.060, "floor_pct": 0.045},
        ],
        "trailing_activation_pct": 0.14,
        "trailing_gap_pct": 0.055,
    }

    # Peak printed +4%; a pullback to +3.2% is still allowed to breathe.
    hold = progressive_profit_lock(
        avg_price=1.0, peak_price=1.04, current_price=1.032, lock_cfg=lock
    )
    assert hold.floor_pct == 0.03
    assert hold.reason is None

    # But a pullback through +3% must lock the win instead of drifting to +1.5%.
    close = progressive_profit_lock(
        avg_price=1.0, peak_price=1.04, current_price=1.029, lock_cfg=lock
    )
    assert close.floor_pct == 0.03
    assert close.stop_price == 1.03
    assert close.reason and "profit lock" in close.reason


def test_progressive_profit_lock_ratchets_higher_for_larger_winners():
    lock = {
        "enabled": True,
        "activation_pct": 0.025,
        "breakeven_floor_pct": 0.012,
        "floor_steps": [
            {"peak_pct": 0.025, "floor_pct": 0.012},
            {"peak_pct": 0.040, "floor_pct": 0.030},
            {"peak_pct": 0.060, "floor_pct": 0.045},
            {"peak_pct": 0.080, "floor_pct": 0.060},
            {"peak_pct": 0.120, "floor_pct": 0.090},
        ],
        "trailing_activation_pct": 0.14,
        "trailing_gap_pct": 0.055,
    }

    six = progressive_profit_lock(
        avg_price=1.0, peak_price=1.06, current_price=1.044, lock_cfg=lock
    )
    assert six.floor_pct == 0.045
    assert six.reason and "floor_pct=0.045" in six.reason

    runner = progressive_profit_lock(
        avg_price=1.0, peak_price=1.16, current_price=1.095, lock_cfg=lock
    )
    assert runner.floor_pct > 0.09
    assert runner.reason and "profit lock" in runner.reason
