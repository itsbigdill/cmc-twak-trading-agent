"""
Execution layer (F5) — the agent's hands (Trust Wallet Agent Kit).

Idempotency is enforced here: we derive a deterministic client_order_id, persist
the order as PENDING *before* sending, and refuse to resend an id we've already
seen. A crash between "begin" and "complete" leaves a PENDING order that the loop
reconciles on restart instead of double-swapping.

Two implementations:
  * MockExecutor — simulates fills with configurable slippage; updates state.
  * TwakExecutor — shells out to the `twak` CLI (verified v0.19.1).

TWAK is SPOT-ONLY (no perps), so the agent is long/cash: buys spend an exact
atomic amount of BSC USDT, closes swap the live token balance back to USDT.
Token legs use contract addresses; slippage is a percent.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Callable, Optional

from .logbook import DecisionLog, utc_now_iso
from .state import Order, PortfolioState, Position, make_order_id


USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"


@dataclass(frozen=True)
class ChainBalance:
    atomic: int
    decimals: Optional[int]

    @property
    def amount(self) -> Decimal:
        if self.decimals is None:
            return Decimal(0)
        return Decimal(self.atomic).scaleb(-self.decimals)


@dataclass
class ExecutionResult:
    status: str                    # filled | skipped | reconciled
    order_id: str
    tx_hash: str = ""
    fill_price: float = 0.0
    quantity: float = 0.0
    quote_amount: float = 0.0
    note: str = ""

    @property
    def filled(self) -> bool:
        return self.status == "filled" and bool(self.tx_hash)


class AmbiguousExecutionError(RuntimeError):
    """The process may have broadcast a transaction; never retry blindly."""

    def __init__(self, message: str, tx_hash: str = ""):
        super().__init__(message)
        self.tx_hash = tx_hash


# --- position bookkeeping (shared) --------------------------------------------
def _apply_spot_buy(state: PortfolioState, token: str, size_usd: float, price: float,
                    opened_ts: float = 0.0):
    if price <= 0:
        raise RuntimeError(f"refusing buy {token}: non-positive fill price {price}")
    qty = size_usd / price
    pos = state.positions.get(token) or Position(token=token)
    new_qty = pos.qty + qty
    pos.avg_price = (pos.avg_price * pos.qty + price * qty) / new_qty if new_qty else price
    pos.qty = new_qty
    pos.is_perp = False
    pos.peak_executable_price = max(pos.peak_executable_price, price)
    if opened_ts and pos.opened_ts <= 0:
        pos.opened_ts = opened_ts
    state.positions[token] = pos
    state.cash_usd -= size_usd


def _apply_live_buy(state: PortfolioState, token: str, spent: float,
                    received: float, quote_balance: float,
                    opened_ts: float = 0.0) -> float:
    if spent <= 0 or received <= 0:
        raise RuntimeError("confirmed buy produced no positive balance delta")
    fill_price = spent / received
    pos = state.positions.get(token) or Position(token=token)
    old_qty = max(0.0, pos.qty)
    new_qty = old_qty + received
    pos.avg_price = ((pos.avg_price * old_qty) + spent) / new_qty
    pos.qty = new_qty
    pos.is_perp = False
    pos.peak_executable_price = max(pos.peak_executable_price, fill_price)
    if opened_ts and pos.opened_ts <= 0:
        pos.opened_ts = opened_ts
    state.positions[token] = pos
    state.cash_usd = quote_balance
    return fill_price


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


def _apply_live_close(state: PortfolioState, token: str, sold: float,
                      proceeds: float, quote_balance: float, *, close_all: bool = True) -> float:
    if sold <= 0 or proceeds <= 0:
        raise RuntimeError("confirmed sell produced no positive balance delta")
    pos = state.positions.get(token)
    fill_price = proceeds / sold
    if pos and not pos.is_perp:
        accounted_qty = min(max(pos.qty, 0.0), sold)
        state.realized_pnl += proceeds - accounted_qty * pos.avg_price
        remaining = max(0.0, pos.qty - accounted_qty)
        if close_all or remaining <= max(1e-12, pos.qty * 1e-5):
            state.positions.pop(token, None)
        else:
            pos.qty = remaining
            state.positions[token] = pos
    elif close_all:
        state.positions.pop(token, None)
    state.cash_usd = quote_balance
    return fill_price


def _apply_short_open(state: PortfolioState, token: str, size_usd: float, price: float, lev: float):
    # perp short: negative notional units; collateral conceptually reserved in cash
    qty = -(size_usd * lev / price)
    pos = Position(token=token, qty=qty, avg_price=price, leverage=lev, is_perp=True)
    state.positions[token] = pos


class MockExecutor:
    def __init__(self, cfg: dict, slippage: float = 0.001):
        self.cfg = cfg
        self.slippage = slippage

    def execute(self, *, tick_id, token, action, size_usd, price, state, log,
                now=None, persist: Optional[Callable[[], None]] = None) -> ExecutionResult:
        oid = make_order_id(tick_id, token, action)
        if state.has_order(oid):
            log.event("skip_duplicate", order_id=oid, token=token, action=action)
            return ExecutionResult("skipped", oid, note="duplicate order")
        order = Order(client_order_id=oid, token=token, action=action,
                      size_usd=size_usd, ts=utc_now_iso())
        state.begin_order(order)
        if persist:
            persist()

        fill_price = price * (1 + self.slippage) if action in ("buy",) else price * (1 - self.slippage)
        if action == "buy":
            _apply_spot_buy(state, token, size_usd, fill_price,
                            now if now is not None else time.time())
        elif action == "short":
            if not self.cfg["risk"].get("perps_enabled"):
                log.event("unsupported_action", token=token, action="short",
                          note="spot-only (perps disabled); short ignored")
                state.fail_order(oid, "perps disabled")
                return ExecutionResult("skipped", oid, note="perps disabled")
            _apply_short_open(state, token, size_usd, fill_price,
                              self.cfg["risk"].get("max_leverage", 1.0))
        elif action in ("close", "sell"):
            _apply_close(state, token, fill_price)
        elif action == "trim":
            pos = state.positions.get(token)
            if not pos or price <= 0:
                return ExecutionResult("skipped", oid, note="no local position")
            qty = min(pos.qty, size_usd / fill_price)
            proceeds = qty * fill_price
            state.realized_pnl += proceeds - qty * pos.avg_price
            pos.qty -= qty
            state.cash_usd += proceeds
            if pos.qty <= 1e-12:
                state.positions.pop(token, None)

        tx = f"0xMOCK{oid}"
        state.complete_order(oid, tx, fill_price)
        state.last_trade_ts = now if now is not None else time.time()
        return ExecutionResult("filled", oid, tx, fill_price,
                               quantity=abs(state.positions.get(token, Position(token)).qty)
                               if action == "buy" else 0.0,
                               quote_amount=size_usd)


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


def _lead_decimal(s, default: Decimal = Decimal(0)) -> Decimal:
    if isinstance(s, (int, float, Decimal)):
        return Decimal(str(s))
    if not isinstance(s, str):
        return default
    try:
        return Decimal(s.strip().split()[0].replace(",", ""))
    except Exception:
        return default


class TwakExecutor:
    """Real SPOT execution via the `twak` CLI (verified v0.19.1, spot-only).

    Buys spend an exact atomic amount of USDT; closes/sells swap the held
    token amount back to USDT. Destination tokens are passed as BSC contract
    addresses; USDT resolves by symbol. Slippage is a percent ("1" = 1%).
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chain = cfg["twak"]["chain"]
        self.quote = cfg["quote_asset"]                       # USDT (symbol leg)
        self.slip = str(cfg["risk"]["max_slippage_pct"] * 100)  # 0.01 -> "1"
        self.contracts = cfg["twak"]["token_contracts"]
        self.address = cfg["twak"]["agent_address"]
        self.quote_contract = cfg["twak"].get("quote_contract", USDT_BSC)
        ex = cfg.get("execution", {})
        self.max_price_impact = Decimal(str(ex.get("max_price_impact_pct", 1.0)))
        self.max_round_trip_loss = Decimal(str(ex.get("max_round_trip_loss_pct", 3.0)))
        self.min_swap_quote = Decimal(str(ex.get("min_swap_quote", 0.25)))
        self.balance_buffer = Decimal(str(ex.get("balance_buffer_fraction", 0.00001)))
        self.reconcile_attempts = max(1, int(ex.get("balance_reconcile_attempts", 5)))
        self.reconcile_interval = max(0.0, float(ex.get("balance_reconcile_interval_seconds", 2)))
        self.min_gas_bnb = Decimal(str(ex.get("min_gas_bnb", 0.003)))
        self._address_verified = False

    def _addr(self, token: str) -> str:
        addr = self.contracts.get(token)
        if not addr:
            raise RuntimeError(f"no BSC contract for {token} (not a trade target)")
        return addr

    def _run(self, args: list[str]) -> dict:
        try:
            proc = subprocess.run(["twak", *args, "--json"], capture_output=True,
                                  text=True, timeout=180)
        except subprocess.TimeoutExpired as e:
            raise AmbiguousExecutionError(
                f"twak timed out after {e.timeout}s; transaction status is unknown"
            ) from e
        # twak prints a human line before the JSON block; the structured result
        # (incl. {error, errorCode} on failure) is the JSON object on stdout.
        out = proc.stdout or ""
        brace = out.find("{")
        data = {}
        if brace >= 0:
            try:
                data = json.loads(out[brace:])
            except Exception:
                data = {}
        if proc.returncode != 0 or data.get("error"):
            code = data.get("errorCode") or data.get("code") or ""
            err = data.get("error") or data.get("message") or proc.stderr.strip() or out.strip()
            raise RuntimeError(f"twak failed{f' [{code}]' if code else ''}: {err}")
        return data

    def _balance(self, token_ref: str) -> ChainBalance:
        """Read an address balance without losing atomic-unit precision.

        TWAK provides both the full-precision human value and integer atomic units.
        Their relationship lets us recover decimals exactly for every non-zero
        balance; a zero balance intentionally has unknown decimals until it changes.
        """
        r = self._run(["balance", "--address", self.address, "--token",
                       token_ref, "--chain", self.chain])
        atomic = int(((r.get("raw") or {}).get("amounts") or {}).get("available") or 0)
        human = Decimal(str(r.get("available") or "0"))
        if atomic == 0 and human == 0:
            return ChainBalance(0, None)
        for dec in range(0, 37):
            if human.scaleb(dec) == Decimal(atomic):
                return ChainBalance(atomic, dec)
        raise RuntimeError(f"cannot infer decimals for {token_ref}: {human=} {atomic=}")

    def _balances(self, token: str) -> tuple[ChainBalance, ChainBalance]:
        return self._balance(self._addr(token)), self._balance(self.quote_contract)

    def _ensure_ready(self) -> None:
        if not self._address_verified:
            wallet = self._run(["wallet", "address", "--chain", self.chain])
            signer = str(wallet.get("address") or "")
            if signer.lower() != self.address.lower():
                raise RuntimeError(
                    f"configured address {self.address} does not match TWAK signer {signer or 'unknown'}"
                )
            self._address_verified = True
        gas = self._run(["wallet", "balance", "--chain", self.chain])
        available = Decimal(str(gas.get("available") or "0"))
        if available < self.min_gas_bnb:
            raise RuntimeError(
                f"gas reserve too low: {available} BNB < {self.min_gas_bnb} BNB"
            )

    def preflight(self) -> None:
        self._ensure_ready()

    @staticmethod
    def _atomic(amount: Decimal, decimals: int) -> int:
        return int(amount.scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))

    def _validate_quote(self, quote: dict) -> None:
        if _lead_decimal(quote.get("output")) <= 0:
            raise RuntimeError("swap quote has no positive output")
        impact = abs(_lead_decimal(str(quote.get("priceImpact", "999")).replace("%", "")))
        if impact > self.max_price_impact:
            raise RuntimeError(
                f"price impact {impact}% exceeds {self.max_price_impact}% limit"
            )

    def _validate_buy_round_trip(self, token: str, spend: Decimal, quote: dict) -> dict:
        """Reject routes that look fine one-way but are economically unsellable.

        Some aggregators report zero price impact even when the reverse route loses
        nearly half the position (ZETA was the live example).  Quote the exact
        expected output back to USDT and enforce recovery independently.
        """
        qty = _lead_decimal(quote.get("output"))
        reverse = self._sell_quote(token, format(qty, "f"))
        self._validate_quote(reverse)
        recovered = _lead_decimal(reverse.get("output"))
        if spend <= 0 or recovered <= 0:
            raise RuntimeError("round-trip quote has no positive recovery")
        loss_pct = max(Decimal(0), (Decimal(1) - recovered / spend) * Decimal(100))
        if loss_pct > self.max_round_trip_loss:
            raise RuntimeError(
                f"round-trip loss {loss_pct:.4f}% exceeds {self.max_round_trip_loss}% limit"
            )
        return {"recovered": str(recovered), "lossPct": str(loss_pct), "reverse": reverse}

    def _wait_for_balance_change(self, token: str, before_token: ChainBalance,
                                 before_quote: ChainBalance, action: str
                                 ) -> tuple[ChainBalance, ChainBalance]:
        last = (before_token, before_quote)
        for attempt in range(self.reconcile_attempts):
            after_token, after_quote = self._balances(token)
            last = (after_token, after_quote)
            if action == "buy":
                changed = (after_token.atomic > before_token.atomic and
                           after_quote.atomic < before_quote.atomic)
            else:
                changed = (after_token.atomic < before_token.atomic and
                           after_quote.atomic > before_quote.atomic)
            if changed:
                return last
            if attempt + 1 < self.reconcile_attempts and self.reconcile_interval:
                time.sleep(self.reconcile_interval)
        return last

    def _prepare_order(self, state: PortfolioState, oid: str, token: str, action: str,
                       size_usd: float, token_balance: ChainBalance,
                       quote_balance: ChainBalance, requested_atomic: int,
                       persist: Optional[Callable[[], None]]) -> None:
        state.begin_order(Order(
            client_order_id=oid, token=token, action=action, size_usd=size_usd,
            ts=utc_now_iso(), pre_token_atomic=token_balance.atomic,
            pre_quote_atomic=quote_balance.atomic, token_decimals=token_balance.decimals,
            quote_decimals=quote_balance.decimals, requested_atomic=requested_atomic,
        ))
        if persist:
            persist()                 # the intent now exists on disk before broadcast

    def _buy_cmd(self, token, quote_amount, quote_only=False) -> list[str]:
        cmd = ["swap", str(quote_amount), self.quote_contract, self._addr(token),
               "--chain", self.chain, "--slippage", self.slip]
        return cmd + (["--quote-only"] if quote_only else [])

    def _sell_cmd(self, token, qty) -> list[str]:
        return ["swap", str(qty), self._addr(token), self.quote_contract,
                "--chain", self.chain, "--slippage", self.slip]

    def quote_only(self, token, quote_amount) -> dict:
        return self._run(self._buy_cmd(token, quote_amount, quote_only=True))

    def _sell_quote(self, token, qty) -> dict:
        return self._run(self._sell_cmd(token, qty) + ["--quote-only"])

    def executable_price(self, token: str) -> float:
        """Current liquidation price from a real reverse quote, or zero on no balance."""
        bal = self._balance(self._addr(token))
        if bal.atomic <= 0 or bal.decimals is None:
            return 0.0
        dust = max(1, int(Decimal(bal.atomic) * self.balance_buffer))
        sell_atomic = bal.atomic - dust
        if sell_atomic <= 0:
            return 0.0
        qty = Decimal(sell_atomic).scaleb(-bal.decimals)
        quote = self._sell_quote(token, format(qty, "f"))
        self._validate_quote(quote)
        proceeds = _lead_decimal(quote.get("output"))
        return float(proceeds / qty) if qty > 0 else 0.0

    def execute(self, *, tick_id, token, action, size_usd, price, state, log,
                now=None, persist: Optional[Callable[[], None]] = None) -> ExecutionResult:
        oid = make_order_id(tick_id, token, action)
        if state.has_order(oid):
            log.event("skip_duplicate", order_id=oid, token=token, action=action)
            return ExecutionResult("skipped", oid, note="duplicate order")

        self._ensure_ready()
        before_token, before_quote = self._balances(token)
        if before_quote.decimals is None:
            raise RuntimeError("quote-asset decimals unavailable")

        if action == "buy":
            requested = self._atomic(Decimal(str(size_usd)), before_quote.decimals)
            reserve = max(1, int(Decimal(before_quote.atomic) * self.balance_buffer))
            spend_atomic = min(requested, max(0, before_quote.atomic - reserve))
            spend = Decimal(spend_atomic).scaleb(-before_quote.decimals)
            if spend < self.min_swap_quote:
                raise RuntimeError(
                    f"buy amount {spend} {self.quote} is below {self.min_swap_quote} minimum"
                )
            amount_text = format(spend, "f")
            q = self.quote_only(token, amount_text)
            self._validate_quote(q)
            round_trip = self._validate_buy_round_trip(token, spend, q)
            log.event("quote", token=token, quote=q, round_trip=round_trip)
            self._prepare_order(state, oid, token, action, float(spend), before_token,
                                before_quote, spend_atomic, persist)
            res = self._run(self._buy_cmd(token, amount_text))
        elif action in ("close", "sell", "trim"):
            pos = state.positions.get(token)
            if not pos or pos.qty <= 0:
                return ExecutionResult("skipped", oid, note="no local position")
            if before_token.atomic <= 0:
                state.positions.pop(token, None)
                state.cash_usd = float(before_quote.amount)
                log.event("close_reconciled", token=token,
                          note="zero on-chain balance; no trade counted")
                return ExecutionResult("reconciled", oid, note="zero on-chain balance")
            if before_token.decimals is None:
                raise RuntimeError("token decimals unavailable")
            dust = max(1, int(Decimal(before_token.atomic) * self.balance_buffer))
            max_sell_atomic = before_token.atomic - dust
            if action == "trim":
                if price <= 0 or size_usd <= 0:
                    return ExecutionResult("skipped", oid, note="invalid trim size/price")
                desired = self._atomic(Decimal(str(size_usd / price)), before_token.decimals)
                sell_atomic = min(max_sell_atomic, max(1, desired))
            else:
                sell_atomic = max_sell_atomic
            qty = Decimal(sell_atomic).scaleb(-before_token.decimals)
            qty_text = format(qty, "f")
            q = self._sell_quote(token, qty_text)
            self._validate_quote(q)
            log.event("quote", token=token, quote=q)
            self._prepare_order(state, oid, token, action, size_usd, before_token,
                                before_quote, sell_atomic, persist)
            res = self._run(self._sell_cmd(token, qty_text))
        else:
            log.event("unsupported_action", token=token, action=action,
                      note="spot-only executor (perps disabled)")
            return ExecutionResult("skipped", oid, note="unsupported action")

        tx = res.get("txHash") or res.get("hash") or res.get("tx_hash", "")
        if not tx:
            raise AmbiguousExecutionError("TWAK reported success without a transaction hash")
        state.mark_sent(oid, tx)
        if persist:
            persist()

        after_token, after_quote = self._wait_for_balance_change(
            token, before_token, before_quote, action
        )
        if action == "buy":
            token_delta = after_token.atomic - before_token.atomic
            quote_delta = before_quote.atomic - after_quote.atomic
        else:
            token_delta = before_token.atomic - after_token.atomic
            quote_delta = after_quote.atomic - before_quote.atomic
        if token_delta <= 0 or quote_delta <= 0 or after_token.decimals is None \
                or after_quote.decimals is None:
            raise AmbiguousExecutionError(
                "transaction confirmed but exact balance deltas are not yet observable", tx
            )

        qty_f = float(Decimal(token_delta).scaleb(-after_token.decimals))
        quote_f = float(Decimal(quote_delta).scaleb(-after_quote.decimals))
        quote_balance_f = float(after_quote.amount)
        if action == "buy":
            fill_price = _apply_live_buy(
                state, token, quote_f, qty_f, quote_balance_f,
                now if now is not None else time.time()
            )
        else:
            fill_price = _apply_live_close(
                state, token, qty_f, quote_f, quote_balance_f,
                close_all=action != "trim"
            )
        state.complete_order(oid, tx, fill_price)
        state.clear_execution_failure(token)
        state.last_trade_ts = now if now is not None else time.time()
        return ExecutionResult("filled", oid, tx, fill_price, qty_f, quote_f)

    def reconcile(self, order: Order, state: PortfolioState) -> Optional[ExecutionResult]:
        """Resolve an interrupted order from persisted pre-trade balance snapshots."""
        if order.pre_token_atomic is None or order.pre_quote_atomic is None:
            state.reconcile_order(order.client_order_id,
                                  "legacy order has no balance snapshots")
            return None
        token_now, quote_now = self._balances(order.token)
        if token_now.decimals is None or quote_now.decimals is None:
            return None
        if order.action == "buy":
            token_delta = token_now.atomic - order.pre_token_atomic
            quote_delta = order.pre_quote_atomic - quote_now.atomic
        else:
            token_delta = order.pre_token_atomic - token_now.atomic
            quote_delta = quote_now.atomic - order.pre_quote_atomic
        if token_delta <= 0 or quote_delta <= 0:
            if order.tx_hash:
                tx = self._run(["tx", order.tx_hash, "--chain", self.chain,
                                "--address", self.address])
                if tx.get("failed"):
                    state.fail_order(order.client_order_id,
                                     "on-chain transaction failed")
                    return ExecutionResult("reconciled", order.client_order_id,
                                           order.tx_hash, note="on-chain transaction failed")
            return None
        qty = float(Decimal(token_delta).scaleb(-token_now.decimals))
        quote = float(Decimal(quote_delta).scaleb(-quote_now.decimals))
        if order.action == "buy":
            fill_price = _apply_live_buy(state, order.token, quote, qty,
                                         float(quote_now.amount))
        else:
            fill_price = _apply_live_close(state, order.token, qty, quote,
                                           float(quote_now.amount),
                                           close_all=order.action != "trim")
        tx = order.tx_hash or f"balance-delta:{order.client_order_id}"
        state.complete_order(order.client_order_id, tx, fill_price)
        state.clear_execution_failure(order.token)
        return ExecutionResult("filled", order.client_order_id, tx, fill_price, qty, quote,
                               note="recovered from persisted balance deltas")

    def reconcile_portfolio(self, state: PortfolioState) -> list[dict]:
        """Make persisted cash/quantities agree with the signer wallet at startup."""
        changes = []
        quote = self._balance(self.quote_contract)
        if quote.decimals is not None:
            actual_cash = float(quote.amount)
            if abs(state.cash_usd - actual_cash) > 1e-12:
                changes.append({"asset": self.quote, "before": state.cash_usd,
                                "after": actual_cash})
                state.cash_usd = actual_cash
        for token in list(state.positions):
            bal = self._balance(self._addr(token))
            actual_qty = float(bal.amount) if bal.decimals is not None else 0.0
            before = state.positions[token].qty
            if actual_qty <= 0:
                state.positions.pop(token, None)
            else:
                state.positions[token].qty = actual_qty
            if abs(before - actual_qty) > 1e-15:
                changes.append({"asset": token, "before": before, "after": actual_qty})
        return changes


def build_executor(cfg: dict):
    # Only true 'live' signs real swaps. 'paper' = real signals, simulated fills
    # (fine-tune on the live market with zero risk); 'dry_run' = fully mocked.
    return TwakExecutor(cfg) if cfg.get("mode") == "live" else MockExecutor(cfg)
