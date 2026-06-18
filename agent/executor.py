"""
Execution layer (F5) — the agent's hands (Trust Wallet Agent Kit).

Idempotency is enforced here: we derive a deterministic client_order_id, persist
the order as PENDING *before* sending, and refuse to resend an id we've already
seen. A crash between "begin" and "complete" leaves a PENDING order that the loop
reconciles on restart instead of double-swapping.

Two implementations:
  * MockExecutor — simulates fills with configurable slippage; updates state.
  * TwakExecutor — shells out to the `twak` CLI in agent-wallet mode.

The real CLI flags (`twak swap ...`, perps open/close) must be confirmed against
the installed TWAK version; the command construction is isolated in _swap_cmd /
_perp_cmd so only those need adjusting.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Optional

from .logbook import DecisionLog, utc_now_iso
from .state import Order, PortfolioState, Position, make_order_id


# --- position bookkeeping (shared) --------------------------------------------
def _apply_spot_buy(state: PortfolioState, token: str, size_usd: float, price: float):
    qty = size_usd / price
    pos = state.positions.get(token) or Position(token=token)
    new_qty = pos.qty + qty
    pos.avg_price = (pos.avg_price * pos.qty + price * qty) / new_qty if new_qty else price
    pos.qty = new_qty
    pos.is_perp = False
    state.positions[token] = pos
    state.cash_usd -= size_usd


def _apply_close(state: PortfolioState, token: str, price: float):
    pos = state.positions.get(token)
    if not pos or pos.qty == 0:
        return
    if pos.is_perp:
        state.cash_usd += pos.qty * (price - pos.avg_price)   # realize perp pnl
    else:
        proceeds = pos.qty * price
        state.cash_usd += proceeds
        state.realized_pnl += pos.qty * (price - pos.avg_price)
    state.positions.pop(token, None)


def _apply_short_open(state: PortfolioState, token: str, size_usd: float, price: float, lev: float):
    # perp short: negative notional units; collateral conceptually reserved in cash
    qty = -(size_usd * lev / price)
    pos = Position(token=token, qty=qty, avg_price=price, leverage=lev, is_perp=True)
    state.positions[token] = pos


class MockExecutor:
    def __init__(self, cfg: dict, slippage: float = 0.001):
        self.cfg = cfg
        self.slippage = slippage

    def execute(self, *, tick_id, token, action, size_usd, price, state, log) -> Optional[str]:
        oid = make_order_id(tick_id, token, action)
        if state.has_order(oid):
            log.event("skip_duplicate", order_id=oid, token=token, action=action)
            return None
        order = Order(client_order_id=oid, token=token, action=action,
                      size_usd=size_usd, ts=utc_now_iso())
        state.begin_order(order)                       # PERSIST-BEFORE-SEND happens in caller

        fill_price = price * (1 + self.slippage) if action in ("buy",) else price * (1 - self.slippage)
        if action == "buy":
            _apply_spot_buy(state, token, size_usd, fill_price)
        elif action == "short":
            _apply_short_open(state, token, size_usd, fill_price, self.cfg["risk"]["max_leverage"])
        elif action in ("close", "sell"):
            _apply_close(state, token, fill_price)

        tx = f"0xMOCK{oid}"
        state.complete_order(oid, tx, fill_price)
        state.last_trade_ts = time.time()
        return tx


class TwakExecutor:
    """Real execution via the `twak` CLI in agent-wallet mode."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.slippage = cfg["risk"]["max_slippage_pct"]
        self.quote = cfg["quote_asset"]

    def _run(self, args: list[str]) -> dict:
        proc = subprocess.run(["twak", *args, "--json"], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"twak failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)

    # --- command construction (CONFIRM flags against installed twak version) ---
    def _swap_cmd(self, frm, to, amount_usd) -> list[str]:
        return ["swap", "--from", frm, "--to", to, "--amount-usd", str(amount_usd),
                "--slippage", str(self.slippage)]

    def _perp_cmd(self, token, side, size_usd, lev) -> list[str]:
        return ["perp", side, "--market", f"{token}-PERP", "--size-usd", str(size_usd),
                "--leverage", str(lev), "--slippage", str(self.slippage)]

    def quote_only(self, frm, to, amount_usd) -> dict:
        return self._run(self._swap_cmd(frm, to, amount_usd) + ["--quote-only"])

    def execute(self, *, tick_id, token, action, size_usd, price, state, log) -> Optional[str]:
        oid = make_order_id(tick_id, token, action)
        if state.has_order(oid):
            log.event("skip_duplicate", order_id=oid, token=token, action=action)
            return None
        state.begin_order(Order(client_order_id=oid, token=token, action=action,
                                size_usd=size_usd, ts=utc_now_iso()))

        # Quote check before sending (slippage/MEV guard).
        if action == "buy":
            q = self.quote_only(self.quote, token, size_usd)
            log.event("quote", token=token, quote=q)
            res = self._run(self._swap_cmd(self.quote, token, size_usd))
            _apply_spot_buy(state, token, size_usd, _fill_px(res, price))
        elif action in ("close", "sell"):
            res = self._run(self._swap_cmd(token, self.quote, size_usd))
            _apply_close(state, token, _fill_px(res, price))
        elif action == "short":
            res = self._run(self._perp_cmd(token, "open-short", size_usd, self.cfg["risk"]["max_leverage"]))
            _apply_short_open(state, token, size_usd, _fill_px(res, price), self.cfg["risk"]["max_leverage"])
        else:
            return None

        tx = res.get("tx_hash") or res.get("hash", "")
        state.complete_order(oid, tx, _fill_px(res, price))
        state.last_trade_ts = time.time()
        return tx


def _fill_px(res: dict, fallback: float) -> float:
    return float(res.get("fill_price") or res.get("price") or fallback)


def build_executor(cfg: dict):
    return TwakExecutor(cfg) if cfg.get("mode") == "live" else MockExecutor(cfg)
