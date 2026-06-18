import os

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
