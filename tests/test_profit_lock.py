import copy

from agent.agent import process_tick
from agent.executor import MockExecutor
from agent.logbook import DecisionLog
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
