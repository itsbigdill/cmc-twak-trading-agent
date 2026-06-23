import os

import pytest

from agent.state import Order, PortfolioState, Position, make_order_id


def test_order_id_is_deterministic():
    a = make_order_id("tick-42", "CAKE", "buy")
    b = make_order_id("tick-42", "CAKE", "buy")
    c = make_order_id("tick-42", "CAKE", "sell")
    assert a == b          # same inputs -> same id (no double submit)
    assert a != c


def test_idempotent_begin_and_has(tmp_path):
    s = PortfolioState(cash_usd=1000)
    oid = make_order_id("tick-1", "BNB", "buy")
    assert not s.has_order(oid)
    s.begin_order(Order(client_order_id=oid, token="BNB", action="buy", size_usd=100))
    assert s.has_order(oid)
    # a restart would see this PENDING and reconcile rather than re-send
    assert len(s.pending_orders()) == 1


def test_drawdown_and_daily_loss():
    s = PortfolioState(cash_usd=1000, peak_equity=1000)
    s.day_start_equity = 1000
    assert s.current_drawdown(800) == 0.2
    assert s.current_drawdown(1100) == 0.0
    assert s.daily_loss(920) == 0.08


def test_position_pnl_pct_long_and_short():
    s = PortfolioState()
    s.positions["CAKE"] = Position(token="CAKE", qty=10, avg_price=2.0)   # long
    s.positions["ETH"] = Position(token="ETH", qty=-1, avg_price=3000.0, is_perp=True)  # short
    assert abs(s.position_pnl_pct("CAKE", 2.2) - 0.10) < 1e-9    # +10% long
    assert abs(s.position_pnl_pct("CAKE", 1.8) + 0.10) < 1e-9    # -10% long
    assert abs(s.position_pnl_pct("ETH", 2700.0) - 0.10) < 1e-9  # short profits on drop


def test_save_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "p.json")
    s = PortfolioState(cash_usd=500, peak_equity=600)
    s.positions["CAKE"] = Position(token="CAKE", qty=10, avg_price=2.0)
    s.begin_order(Order(client_order_id="abc", token="CAKE", action="buy", size_usd=20))
    s.save(path)
    loaded = PortfolioState.load(path)
    assert loaded.cash_usd == 500
    assert loaded.positions["CAKE"].qty == 10
    assert loaded.open_orders["abc"].status == "PENDING"


def test_confirmed_fill_requires_hash_and_counts_once():
    s = PortfolioState()
    s.begin_order(Order(client_order_id="abc", token="CAKE", action="buy", size_usd=2))
    with pytest.raises(ValueError):
        s.complete_order("abc", "", 1.0)
    assert s.trade_count_total == 0
    s.complete_order("abc", "0xtx", 1.0)
    s.complete_order("abc", "0xtx", 1.0)
    assert s.trade_count_total == 1
    assert s.trades_today == 1
    assert s.entries_today == 1


def test_unknown_order_blocks_token_until_reconciled():
    s = PortfolioState()
    s.begin_order(Order(client_order_id="abc", token="CAKE", action="buy", size_usd=2))
    s.fail_order("abc", "timeout", unknown=True)
    assert s.has_unresolved_order("CAKE")
    s.reconcile_order("abc", "manual review")
    assert not s.has_unresolved_order("CAKE")


def test_execution_failure_backoff_is_exponential_and_clearable():
    s = PortfolioState()
    assert s.record_execution_failure("CAKE", 100, 60, 900) == 160
    assert s.record_execution_failure("CAKE", 100, 60, 900) == 220
    s.clear_execution_failure("CAKE")
    assert "CAKE" not in s.execution_retry_after
