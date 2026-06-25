from types import SimpleNamespace

import pytest

from agent.agent import _exec_and_log
from agent.executor import (
    AmbiguousExecutionError,
    ChainBalance,
    ExecutionResult,
    TwakExecutor,
    _apply_live_close,
)
from agent.state import PortfolioState, Position, make_order_id
from agent.state import Order


class MemoryLog:
    def __init__(self):
        self.rows = []

    def event(self, kind, **fields):
        self.rows.append({"kind": kind, **fields})


def live_cfg(cfg, tmp_path):
    cfg = {**cfg, "mode": "live", "execution": {
        "max_price_impact_pct": 1.0,
        "max_round_trip_loss_pct": 3.0,
        "min_swap_quote": 0.25,
        "balance_buffer_fraction": 0.00001,
        "balance_reconcile_attempts": 1,
        "balance_reconcile_interval_seconds": 0,
        "min_gas_bnb": 0.003,
        "retry_base_seconds": 60,
        "retry_max_seconds": 900,
    }, "paths": {**cfg["paths"], "state_file": str(tmp_path / "state.json")}}
    cfg["twak"] = {**cfg["twak"],
                   "token_contracts": {
                       "RAY": "0x13b6A55662f6591f8B8408Af1C73B017E32eEdB8"
                   }}
    return cfg


def test_live_buy_uses_confirmed_balance_deltas_and_persists_before_send(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    ex = TwakExecutor(cfg)
    ex._ensure_ready = lambda: None
    balances = iter([
        (ChainBalance(0, None), ChainBalance(10 * 10**18, 18)),
        (ChainBalance(5 * 10**18, 18), ChainBalance(8 * 10**18, 18)),
    ])
    ex._balances = lambda token: next(balances)
    ex.quote_only = lambda token, amount: {
        "input": f"{amount} USDT", "output": "5 RAY", "priceImpact": "0"
    }
    ex._sell_quote = lambda token, qty: {
        "input": f"{qty} RAY", "output": "1.98 USDT", "priceImpact": "0"
    }
    persisted = []
    sent_args = []
    state = PortfolioState(cash_usd=999)

    def run(args):
        # The PENDING intent must already be durable before this broadcast call.
        oid = make_order_id("tick", "RAY", "buy")
        assert state.open_orders[oid].status == "PENDING"
        assert persisted
        sent_args.append(args)
        return {"hash": "0xbuy"}

    ex._run = run
    result = ex.execute(tick_id="tick", token="RAY", action="buy", size_usd=2,
                        price=0.5, state=state, log=MemoryLog(), now=10,
                        persist=lambda: persisted.append(True))

    assert result.filled
    assert result.tx_hash == "0xbuy"
    assert result.quantity == 5
    assert result.quote_amount == 2
    assert state.cash_usd == 8
    assert state.positions["RAY"].qty == 5
    assert state.positions["RAY"].avg_price == 0.4
    assert state.trade_count_total == 1
    assert sent_args[0][0:3] == ["swap", "2.000000000000000000", ex.quote_contract]
    assert len(persisted) == 2                 # PENDING, then SENT


def test_live_sell_uses_real_balance_leaves_bounded_dust_and_books_actual_proceeds(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    ex = TwakExecutor(cfg)
    ex._ensure_ready = lambda: None
    balances = iter([
        (ChainBalance(1_000_000, 6), ChainBalance(8 * 10**18, 18)),
        (ChainBalance(10, 6), ChainBalance(85 * 10**17, 18)),
    ])
    ex._balances = lambda token: next(balances)
    ex._sell_quote = lambda token, qty: {
        "input": f"{qty} RAY", "output": "0.5 USDT", "priceImpact": "0"
    }
    sent_args = []
    ex._run = lambda args: sent_args.append(args) or {"hash": "0xsell"}
    state = PortfolioState(cash_usd=100)
    state.positions["RAY"] = Position(token="RAY", qty=1, avg_price=0.4)

    result = ex.execute(tick_id="tick", token="RAY", action="close", size_usd=0,
                        price=0.5, state=state, log=MemoryLog(), now=10,
                        persist=lambda: None)

    assert result.filled
    assert result.quantity == pytest.approx(0.99999)
    assert result.quote_amount == 0.5
    assert state.cash_usd == 8.5
    assert "RAY" not in state.positions
    assert state.realized_pnl == pytest.approx(0.5 - 0.99999 * 0.4)
    assert sent_args[0][1] == "0.999990"


def test_live_buy_is_capped_below_actual_usdt_balance(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    ex = TwakExecutor(cfg)
    ex._ensure_ready = lambda: None
    balances = iter([
        (ChainBalance(0, None), ChainBalance(10**18, 18)),
        (ChainBalance(2 * 10**18, 18), ChainBalance(10**13, 18)),
    ])
    ex._balances = lambda token: next(balances)
    quoted = []
    ex.quote_only = lambda token, amount: quoted.append(amount) or {
        "output": "2 RAY", "priceImpact": "0"
    }
    ex._sell_quote = lambda token, qty: {
        "output": "0.98 USDT", "priceImpact": "0"
    }
    ex._run = lambda args: {"hash": "0xbuy"}
    state = PortfolioState(cash_usd=50)

    result = ex.execute(tick_id="tick", token="RAY", action="buy", size_usd=2,
                        price=0.5, state=state, log=MemoryLog(), persist=lambda: None)

    assert result.filled
    assert quoted == ["0.999990000000000000"]
    assert result.quote_amount == pytest.approx(0.99999)
    assert state.cash_usd == pytest.approx(0.00001)


def test_zero_chain_balance_reconciles_without_fake_fill_or_cash(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    ex = TwakExecutor(cfg)
    ex._ensure_ready = lambda: None
    ex._balances = lambda token: (ChainBalance(0, None), ChainBalance(7 * 10**18, 18))
    state = PortfolioState(cash_usd=123, trade_count_total=4, trades_today=2)
    state.positions["RAY"] = Position(token="RAY", qty=5, avg_price=1)

    result = ex.execute(tick_id="tick", token="RAY", action="close", size_usd=0,
                        price=2, state=state, log=MemoryLog(), persist=lambda: None)

    assert result.status == "reconciled"
    assert state.cash_usd == 7
    assert "RAY" not in state.positions
    assert state.trade_count_total == 4
    assert state.trades_today == 2
    assert not state.open_orders


def test_twak_error_prefers_structured_stdout(monkeypatch, cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))
    monkeypatch.setattr("agent.executor.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=1,
        stdout='{"error":"ERC20: transfer amount exceeds balance","errorCode":"TX_FAILED"}',
        stderr="Swapping 1 RAY via 0x",
    ))
    with pytest.raises(RuntimeError, match=r"\[TX_FAILED\].*exceeds balance"):
        ex._run(["swap"])


def test_timeout_is_ambiguous_not_a_normal_failure(monkeypatch, cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))

    def timeout(*args, **kwargs):
        raise __import__("subprocess").TimeoutExpired("twak", 180)

    monkeypatch.setattr("agent.executor.subprocess.run", timeout)
    with pytest.raises(AmbiguousExecutionError):
        ex._run(["swap"])


def test_orchestrator_does_not_log_or_count_skipped_execution(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    state = PortfolioState(cash_usd=10)
    log = MemoryLog()

    class SkipExecutor:
        def execute(self, **kwargs):
            return ExecutionResult("skipped", "oid", note="duplicate")

    ok = _exec_and_log(SkipExecutor(), state, cfg, "tick", "RAY", "buy",
                       2, 0.5, log, "test", now=10)

    assert not ok
    assert state.trade_count_total == 0
    assert not any(r["kind"] == "fill" for r in log.rows)
    assert any(r["kind"] == "execution_noop" for r in log.rows)


def test_full_exit_persists_reentry_cooldown(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    state = PortfolioState(cash_usd=10)
    log = MemoryLog()

    class FilledExecutor:
        def execute(self, **kwargs):
            return ExecutionResult("filled", "oid", "0xhash", 1.0, 2.0, 2.0)

    ok = _exec_and_log(FilledExecutor(), state, cfg, "tick", "RAY", "close",
                       0, 0.5, log, "health exit", now=1234)

    assert ok
    assert state.rotation_exited_at["RAY"] == 1234
    saved = PortfolioState.load(cfg["paths"]["state_file"])
    assert saved.rotation_exited_at["RAY"] == 1234


def test_trim_does_not_arm_reentry_cooldown(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    state = PortfolioState(cash_usd=10)
    log = MemoryLog()

    class FilledExecutor:
        def execute(self, **kwargs):
            return ExecutionResult("filled", "oid", "0xhash", 1.0, 1.0, 1.0)

    ok = _exec_and_log(FilledExecutor(), state, cfg, "tick", "RAY", "trim",
                       1, 0.5, log, "micro profit", now=1234)

    assert ok
    assert "RAY" not in state.rotation_exited_at


def test_ambiguous_execution_blocks_token_and_does_not_count_fill(cfg, tmp_path):
    cfg = live_cfg(cfg, tmp_path)
    state = PortfolioState(cash_usd=10)
    log = MemoryLog()

    class UnknownExecutor:
        def execute(self, **kwargs):
            state.begin_order(__import__("agent.state", fromlist=["Order"]).Order(
                client_order_id=make_order_id("tick", "RAY", "buy"),
                token="RAY", action="buy", size_usd=2,
            ))
            kwargs["persist"]()
            raise AmbiguousExecutionError("timeout after possible broadcast", "0xmaybe")

    ok = _exec_and_log(UnknownExecutor(), state, cfg, "tick", "RAY", "buy",
                       2, 0.5, log, "test", now=10)

    oid = make_order_id("tick", "RAY", "buy")
    assert not ok
    assert state.open_orders[oid].status == "UNKNOWN"
    assert state.open_orders[oid].tx_hash == "0xmaybe"
    assert state.has_unresolved_order("RAY")
    assert state.trade_count_total == 0
    assert any(r["kind"] == "exec_unknown" for r in log.rows)


def test_restart_reconciles_pending_buy_from_atomic_balance_deltas(cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))
    state = PortfolioState(cash_usd=10)
    order = Order(client_order_id="oid", token="RAY", action="buy", size_usd=2,
                  pre_token_atomic=0, pre_quote_atomic=10 * 10**18,
                  quote_decimals=18)
    state.begin_order(order)
    ex._balances = lambda token: (
        ChainBalance(4 * 10**6, 6), ChainBalance(8 * 10**18, 18)
    )

    result = ex.reconcile(order, state)

    assert result.filled
    assert result.tx_hash == "balance-delta:oid"
    assert state.open_orders["oid"].status == "FILLED"
    assert state.positions["RAY"].qty == 4
    assert state.cash_usd == 8
    assert state.trade_count_total == 1


def test_quote_guard_rejects_excessive_price_impact(cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))
    with pytest.raises(RuntimeError, match="price impact"):
        ex._validate_quote({"output": "10 RAY", "priceImpact": "1.01"})


def test_round_trip_guard_rejects_zero_impact_honeypot_route(cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))
    ex._sell_quote = lambda token, qty: {
        "input": f"{qty} RAY", "output": "2.05 USDT", "priceImpact": "0"
    }
    with pytest.raises(RuntimeError, match="round-trip loss"):
        ex._validate_buy_round_trip(
            "RAY", __import__("decimal").Decimal("4"),
            {"output": "96 RAY", "priceImpact": "0"},
        )


def test_preflight_rejects_wrong_signing_wallet(cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))
    ex._run = lambda args: {"address": "0x0000000000000000000000000000000000000001"}
    with pytest.raises(RuntimeError, match="does not match TWAK signer"):
        ex.preflight()


def test_startup_portfolio_reconciliation_uses_live_cash_and_quantity(cfg, tmp_path):
    ex = TwakExecutor(live_cfg(cfg, tmp_path))
    state = PortfolioState(cash_usd=99)
    state.positions["RAY"] = Position(token="RAY", qty=2, avg_price=0.5)
    balances = {
        ex.quote_contract.lower(): ChainBalance(8 * 10**18, 18),
        ex._addr("RAY").lower(): ChainBalance(6_479_305, 6),
    }
    ex._balance = lambda token: balances[token.lower()]

    changes = ex.reconcile_portfolio(state)

    assert state.cash_usd == 8
    assert state.positions["RAY"].qty == 6.479305
    assert {c["asset"] for c in changes} == {"USDT", "RAY"}


def test_partial_live_close_preserves_remaining_position():
    state = PortfolioState(cash_usd=5)
    state.positions["RAY"] = Position(token="RAY", qty=10, avg_price=1,
                                      peak_executable_price=1.5)
    px = _apply_live_close(state, "RAY", sold=2, proceeds=2.4,
                           quote_balance=7.4, close_all=False)
    assert px == pytest.approx(1.2)
    assert state.positions["RAY"].qty == pytest.approx(8)
    assert state.positions["RAY"].peak_executable_price == 1.5
    assert state.realized_pnl == pytest.approx(0.4)
