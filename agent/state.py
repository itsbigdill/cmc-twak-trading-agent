"""
Persistent portfolio state (F6) + idempotency.

Why idempotency matters: the agent runs 24/7 on mainnet for a week. A crash or
restart mid-swap must NEVER double-submit a trade. We persist a pending order
(with a deterministic client_order_id) BEFORE sending it; on restart, any order
left in PENDING is reconciled, not re-sent.

Also tracks everything the risk gate and reporting need:
  * realized PnL, equity curve, peak equity (for drawdown)
  * per-day loss and trade count (reset on UTC day boundary)
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from typing import Optional


@dataclass
class Position:
    token: str
    qty: float = 0.0            # token units (negative => short via perps)
    avg_price: float = 0.0      # entry vwap in quote asset
    leverage: float = 1.0
    is_perp: bool = False
    peak_executable_price: float = 0.0
    profit_taken: bool = False
    opened_ts: float = 0.0      # epoch seconds for entry-age aware health exits


@dataclass
class Order:
    client_order_id: str        # deterministic; the idempotency key
    token: str
    action: str                 # buy | sell | close | short
    size_usd: float
    status: str = "PENDING"     # PENDING | SENT | FILLED | FAILED | UNKNOWN | RECONCILE
    tx_hash: Optional[str] = None
    price: Optional[float] = None
    ts: str = ""
    error: Optional[str] = None
    pre_token_atomic: Optional[int] = None
    pre_quote_atomic: Optional[int] = None
    token_decimals: Optional[int] = None
    quote_decimals: Optional[int] = None
    requested_atomic: Optional[int] = None


def make_order_id(tick_id: str, token: str, action: str) -> str:
    """Deterministic id: same tick + token + action => same id => no dupes."""
    h = hashlib.sha256(f"{tick_id}|{token}|{action}".encode()).hexdigest()
    return h[:16]


@dataclass
class PortfolioState:
    cash_usd: float = 0.0
    initial_equity: float = 0.0
    peak_equity: float = 0.0
    realized_pnl: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    open_orders: dict[str, Order] = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)   # [[iso_ts, equity], ...]
    trade_count_total: int = 0
    tick_n: int = 0             # total ticks processed (for periodic actions, e.g. x402)
    halted: bool = False        # kill switch tripped -> no trading for the window
    # per-day, keyed by UTC date string
    day: str = ""
    day_start_equity: float = 0.0
    trades_today: int = 0
    entries_today: int = 0
    last_trade_ts: float = 0.0
    x402_bias: float = 0.0       # premium market bias from the last x402 signal (used in scoring)
    x402_tokens: dict[str, float] = field(default_factory=dict)  # token -> paid signal score
    execution_failures: dict[str, int] = field(default_factory=dict)
    execution_retry_after: dict[str, float] = field(default_factory=dict)
    signal_streaks: dict[str, int] = field(default_factory=dict)

    # ----- equity / drawdown -------------------------------------------------
    def mark_equity(self, mark_prices: dict[str, float], iso_ts: str) -> float:
        """Recompute total equity from cash + open positions at given prices."""
        eq = self.cash_usd
        for p in self.positions.values():
            px = mark_prices.get(p.token, p.avg_price)
            if p.is_perp:
                # PnL on notional; collateral already reflected in cash
                eq += p.qty * (px - p.avg_price)
            else:
                eq += p.qty * px
        eq = round(eq, 6)
        self.peak_equity = max(self.peak_equity, eq)
        self.equity_curve.append([iso_ts, eq])
        return eq

    def position_pnl_pct(self, token: str, price: float) -> float:
        """Unrealized PnL of a position as a fraction of entry. Handles shorts."""
        p = self.positions.get(token)
        if not p or p.avg_price <= 0:
            return 0.0
        if p.qty < 0:                       # short (perp): profit when price falls
            return (p.avg_price - price) / p.avg_price
        return (price - p.avg_price) / p.avg_price

    def current_drawdown(self, equity: float) -> float:
        """Peak-to-now drawdown as a fraction (0..1). 0 if at/above peak."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity)

    def daily_loss(self, equity: float) -> float:
        """Loss since start of UTC day, as fraction. 0 if up on the day."""
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - equity) / self.day_start_equity)

    def roll_day(self, utc_date: str, equity: float) -> None:
        if utc_date != self.day:
            self.day = utc_date
            self.day_start_equity = equity
            self.trades_today = 0
            self.entries_today = 0
            self.halted = False          # kill switch re-arms each UTC day (not a
                                         # permanent window halt) so we can recover

    # ----- idempotent order lifecycle ---------------------------------------
    def begin_order(self, order: Order) -> None:
        self.open_orders[order.client_order_id] = order

    def has_order(self, client_order_id: str) -> bool:
        return client_order_id in self.open_orders

    def complete_order(self, client_order_id: str, tx_hash: str, price: float) -> None:
        o = self.open_orders.get(client_order_id)
        if not o:
            return
        if o.status == "FILLED":
            return
        if not tx_hash:
            raise ValueError("a confirmed fill requires a transaction hash")
        o.status = "FILLED"
        o.tx_hash = tx_hash
        o.price = price
        o.error = None
        self.trade_count_total += 1
        self.trades_today += 1
        if o.action in ("buy", "short"):
            self.entries_today += 1

    def mark_sent(self, client_order_id: str, tx_hash: str) -> None:
        o = self.open_orders.get(client_order_id)
        if not o:
            return
        o.status = "SENT"
        o.tx_hash = tx_hash

    def fail_order(self, client_order_id: str, error: str, *, unknown: bool = False) -> None:
        o = self.open_orders.get(client_order_id)
        if not o:
            return
        o.status = "UNKNOWN" if unknown else "FAILED"
        o.error = error

    def reconcile_order(self, client_order_id: str, note: str = "") -> None:
        o = self.open_orders.get(client_order_id)
        if not o:
            return
        o.status = "RECONCILE"
        o.error = note or o.error

    def pending_orders(self) -> list[Order]:
        return [o for o in self.open_orders.values()
                if o.status in ("PENDING", "SENT", "UNKNOWN")]

    def has_unresolved_order(self, token: str) -> bool:
        return any(o.token == token and o.status in ("PENDING", "SENT", "UNKNOWN")
                   for o in self.open_orders.values())

    def record_execution_failure(self, token: str, now: float, base: float, cap: float) -> float:
        count = self.execution_failures.get(token, 0) + 1
        self.execution_failures[token] = count
        delay = min(cap, base * (2 ** (count - 1)))
        retry_at = now + delay
        self.execution_retry_after[token] = retry_at
        return retry_at

    def clear_execution_failure(self, token: str) -> None:
        self.execution_failures.pop(token, None)
        self.execution_retry_after.pop(token, None)

    # ----- persistence (atomic write) ---------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = asdict(self)
        # dataclasses inside dicts are already converted by asdict
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)   # atomic — no half-written state on crash

    @classmethod
    def load(cls, path: str) -> "PortfolioState":
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            data = json.load(f)
        # Tolerate schema drift: drop unknown keys so a field added/removed in a
        # mid-window deploy can never crash-loop the agent on `cls(**data)`.
        def _keep(d: dict, klass) -> dict:
            allowed = {f.name for f in fields(klass)}
            return {k: v for k, v in d.items() if k in allowed}
        data["positions"] = {k: Position(**_keep(v, Position))
                             for k, v in data.get("positions", {}).items()}
        data["open_orders"] = {k: Order(**_keep(v, Order))
                              for k, v in data.get("open_orders", {}).items()}
        return cls(**_keep(data, cls))
