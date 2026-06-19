"""
Execution layer (F5) — the agent's hands (Trust Wallet Agent Kit).

Idempotency is enforced here: we derive a deterministic client_order_id, persist
the order as PENDING *before* sending, and refuse to resend an id we've already
seen. A crash between "begin" and "complete" leaves a PENDING order that the loop
reconciles on restart instead of double-swapping.

Two implementations:
  * MockExecutor — simulates fills with configurable slippage; updates state.
  * TwakExecutor — shells out to the `twak` CLI (verified v0.19.1).

TWAK is SPOT-ONLY (no perps), so the agent is long/cash: buys spend USDT via
`twak swap --usd`, closes swap the held token back to USDT. Destination tokens
are BSC contract addresses; USDT resolves by symbol; slippage is a percent.
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
    if price <= 0:
        raise RuntimeError(f"refusing buy {token}: non-positive fill price {price}")
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
    if price <= 0:
        price = pos.avg_price        # no live mark -> value the close flat, not at 0
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

    def execute(self, *, tick_id, token, action, size_usd, price, state, log, now=None) -> Optional[str]:
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
        state.last_trade_ts = now if now is not None else time.time()
        return tx


def _lead_float(s, default: float = 0.0) -> float:
    """First number out of strings like '7.534575 CAKE' -> 7.534575."""
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return default
    tok = s.strip().split()
    try:
        return float(tok[0].replace(",", ""))
    except (ValueError, IndexError):
        return default


class TwakExecutor:
    """Real SPOT execution via the `twak` CLI (verified v0.19.1, spot-only).

    Buys spend a USD-equivalent of USDT (`--usd`); closes/sells swap the held
    token amount back to USDT. Destination tokens are passed as BSC contract
    addresses; USDT resolves by symbol. Slippage is a percent ("1" = 1%).
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chain = cfg["twak"]["chain"]
        self.quote = cfg["quote_asset"]                       # USDT (symbol leg)
        self.slip = str(cfg["risk"]["max_slippage_pct"] * 100)  # 0.01 -> "1"
        self.contracts = cfg["twak"]["token_contracts"]

    def _addr(self, token: str) -> str:
        addr = self.contracts.get(token)
        if not addr:
            raise RuntimeError(f"no BSC contract for {token} (not a trade target)")
        return addr

    def _run(self, args: list[str]) -> dict:
        proc = subprocess.run(["twak", *args, "--json"], capture_output=True,
                              text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"twak failed: {proc.stderr.strip() or proc.stdout.strip()}")
        # twak prints a human line before the JSON block; take the JSON object.
        out = proc.stdout
        brace = out.find("{")
        return json.loads(out[brace:]) if brace >= 0 else {}

    def _buy_cmd(self, token, size_usd, quote_only=False) -> list[str]:
        cmd = ["swap", "--usd", str(round(size_usd, 6)), self.quote, self._addr(token),
               "--chain", self.chain, "--slippage", self.slip]
        return cmd + (["--quote-only"] if quote_only else [])

    def _sell_cmd(self, token, qty) -> list[str]:
        return ["swap", str(qty), self._addr(token), self.quote,
                "--chain", self.chain, "--slippage", self.slip]

    def quote_only(self, token, size_usd) -> dict:
        return self._run(self._buy_cmd(token, size_usd, quote_only=True))

    def execute(self, *, tick_id, token, action, size_usd, price, state, log, now=None) -> Optional[str]:
        oid = make_order_id(tick_id, token, action)
        if state.has_order(oid):
            log.event("skip_duplicate", order_id=oid, token=token, action=action)
            return None
        state.begin_order(Order(client_order_id=oid, token=token, action=action,
                                size_usd=size_usd, ts=utc_now_iso()))

        if action == "buy":
            q = self.quote_only(token, size_usd)              # slippage/MEV guard
            log.event("quote", token=token, quote=q)
            res = self._run(self._buy_cmd(token, size_usd))
            out_tokens = _lead_float(res.get("output"))
            fill_px = (size_usd / out_tokens) if out_tokens else price
            _apply_spot_buy(state, token, size_usd, fill_px)
        elif action in ("close", "sell"):
            pos = state.positions.get(token)
            if not pos or pos.qty <= 0:
                return None
            res = self._run(self._sell_cmd(token, pos.qty))
            usdt_out = _lead_float(res.get("output"))
            fill_px = (usdt_out / pos.qty) if pos.qty else price
            _apply_close(state, token, fill_px)
        else:
            log.event("unsupported_action", token=token, action=action,
                      note="spot-only executor (perps disabled)")
            return None

        tx = res.get("txHash") or res.get("hash") or res.get("tx_hash", "")
        state.complete_order(oid, tx, _lead_float(res.get("output")) or price)
        state.last_trade_ts = now if now is not None else time.time()
        return tx


def build_executor(cfg: dict):
    # Only true 'live' signs real swaps. 'paper' = real signals, simulated fills
    # (fine-tune on the live market with zero risk); 'dry_run' = fully mocked.
    return TwakExecutor(cfg) if cfg.get("mode") == "live" else MockExecutor(cfg)
