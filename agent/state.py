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
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Position:
    token: str
    qty: float = 0.0            # token units (negative => short via perps)
    avg_price: float = 0.0      # entry vwap in quote asset
    leverage: float = 1.0
    is_perp: bool = False


@dataclass
class Order:
    client_order_id: str        # deterministic; the idempotency key
    token: str
    action: str                 # buy | sell | close | short
    size_usd: float
    status: str = "PENDING"     # PENDING | FILLED | FAILED | RECONCILE
    tx_hash: Optional[str] = None
    price: Optional[float] = None
    ts: str = ""


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
    # per-day, keyed by UTC date string
    day: str = ""
    day_start_equity: float = 0.0
    trades_today: int = 0
    last_trade_ts: float = 0.0

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

    # ----- idempotent order lifecycle ---------------------------------------
    def begin_order(self, order: Order) -> None:
        self.open_orders[order.client_order_id] = order

    def has_order(self, client_order_id: str) -> bool:
        return client_order_id in self.open_orders

    def complete_order(self, client_order_id: str, tx_hash: str, price: float) -> None:
        o = self.open_orders.get(client_order_id)
        if not o:
            return
        o.status = "FILLED"
        o.tx_hash = tx_hash
        o.price = price
        self.trade_count_total += 1
        self.trades_today += 1

    def pending_orders(self) -> list[Order]:
        return [o for o in self.open_orders.values() if o.status == "PENDING"]

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
        data["positions"] = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        data["open_orders"] = {k: Order(**v) for k, v in data.get("open_orders", {}).items()}
        return cls(**data)
