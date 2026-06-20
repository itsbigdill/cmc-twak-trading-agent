"""
Main loop (orchestrator).

  data (CMC) -> signal -> decision (LLM) -> risk gate -> execute (TWAK) -> log -> persist

Run a single tick:        python -m agent.agent --once
Run continuously:         python -m agent.agent
Discover CMC tool names:  python -m agent.agent --list-cmc-tools   (live only)

Robustness: each tick is wrapped; on failure we log, back off, and continue.
On startup we reconcile any PENDING order from a crash so we never double-submit.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import yaml

from . import risk_gate
from .cmc_client import build_cmc_client
from .signal_source import build_signal_source
from .decision import build_decider
from .executor import build_executor
from .logbook import DecisionLog, utc_date, utc_hour, utc_now_iso
from .signal_engine import score_universe
from .state import PortfolioState


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Optionally load the broad, filter-verified trade universe from a file
    # (scripts/build_universe.py) instead of the inline curated set.
    cf = cfg["twak"].get("contracts_file")
    if cf:
        import json as _json
        p = cf if os.path.isabs(cf) else os.path.join(os.path.dirname(os.path.abspath(path)), cf)
        if os.path.exists(p):
            with open(p) as cff:
                loaded = _json.load(cff)
            if loaded:
                cfg["twak"]["token_contracts"] = loaded
    # Signal universe MUST equal the trade universe, else the strategy can only
    # act on tokens it also has signals for. Derive it from token_contracts so
    # the two can never drift (benchmark drives regime; quote is the cash leg).
    bench = cfg["regime"]["benchmark"]
    trade = list(cfg["twak"]["token_contracts"])
    cfg["whitelist"] = [bench] + [t for t in trade if t != bench] + [cfg["quote_asset"]]
    # Mode is overridable by env so a server can run paper/live without editing
    # (and conflicting with) the repo's config.yaml on git pull.
    cfg["mode"] = os.environ.get("AGENT_MODE", cfg.get("mode", "dry_run"))
    return cfg


def reconcile(state: PortfolioState, log: DecisionLog) -> None:
    """On startup, any PENDING order is from a crash mid-send. Mark for review.

    In dry-run we can't query the chain, so we flag them RECONCILE and do NOT
    resend. In live, this is where you'd query the tx by client_order_id/nonce.
    """
    pend = state.pending_orders()
    for o in pend:
        o.status = "RECONCILE"
        log.event("reconcile", order_id=o.client_order_id, token=o.token,
                  action=o.action, note="left PENDING by prior run; not resent")


def _x402_signal(cfg, log) -> None:
    """Pay-per-request for a premium market signal via TWAK x402 (CDP facilitator).
    Real on-chain micro-payment as part of the trade loop (Best-TWAK rubric).
    No-op unless x402.signal_url is configured."""
    import json
    import subprocess
    url = os.environ.get("X402_SIGNAL_URL") or cfg.get("x402", {}).get("signal_url")
    if not url:
        return
    import re
    cap = str(cfg["x402"].get("max_payment_atomic", 1000))
    try:
        out = subprocess.run(["twak", "x402", "request", url, "--max-payment", cap,
                              "--yes", "--json"], capture_output=True, text=True, timeout=90)
        raw = (out.stdout or "") + (out.stderr or "")
        data = json.loads(out.stdout[out.stdout.find("{"):]) if "{" in out.stdout else {}
        # txHash for the on-chain settlement may live under various keys or only in
        # twak's human output — fall back to scanning the raw text for a 0x tx hash.
        tx = (data.get("txHash") or data.get("transactionHash") or data.get("hash")
              or data.get("tx") or data.get("settlement"))
        if not tx:
            m = re.search(r"0x[0-9a-fA-F]{64}", raw)
            tx = m.group(0) if m else None
        log.event("x402", url=url, tx=tx, paid=data.get("amountPaid") or cap,
                  signal=data.get("data") or data)
    except Exception as e:
        log.event("x402_error", error=str(e))


def _exec_and_log(executor, state, cfg, tick_id, token, action, size_usd, price, log, reason, now=None) -> None:
    """Persist-before-send, execute, log fill or error, persist again."""
    state.save(cfg["paths"]["state_file"])      # idempotency: record intent first
    before = state.realized_pnl
    try:
        tx = executor.execute(tick_id=tick_id, token=token, action=action,
                              size_usd=size_usd, price=price, state=state, log=log, now=now)
        # realized P&L booked by this trade (closes/sells); ~0 for opens
        realized = round(state.realized_pnl - before, 4)
        # fill price: exact entry (avg_price) for a buy; the mark at exit for a close
        pos = state.positions.get(token)
        fill_price = round(pos.avg_price if (action == "buy" and pos) else (price or 0.0), 8)
        log.event("fill", token=token, action=action, size_usd=size_usd,
                  tx_hash=tx, reason=reason, realized=realized, fill_price=fill_price)
    except Exception as e:
        log.event("exec_error", token=token, action=action, error=str(e))
    finally:
        state.save(cfg["paths"]["state_file"])


def _force_close(executor, state, cfg, tick_id, token, price, log, reason, now=None) -> None:
    _exec_and_log(executor, state, cfg, tick_id, token, "close", 0.0, price, log, reason, now=now)


def _mark_px(prices, state, token) -> float:
    """A safe price to value a close: the live price if we have one this tick,
    else the position's entry price. Never 0 (a 0 mark realizes a phantom total
    loss in bookkeeping and can cascade into a false kill)."""
    px = prices.get(token, 0.0)
    if px and px > 0:
        return px
    pos = state.positions.get(token)
    return pos.avg_price if pos else 0.0


def run_tick(cfg, state, cmc, decider, executor, log) -> None:
    """Live tick: fetch market data, then process it."""
    snapshot = cmc.get_snapshot(cfg["whitelist"])
    prices = {t: d.get("price", 0.0) for t, d in snapshot.items()}
    process_tick(cfg, state, snapshot, prices, decider, executor, log)


def process_tick(cfg, state, snapshot, prices, decider, executor, log,
                 *, now_ts=None, date_str=None, hour=None, ts_iso=None) -> None:
    """Core decision/risk/execute path. Clock is injectable so the backtester
    can replay historical bars through the exact same logic as the live loop."""
    import time as _time

    now_ts = now_ts if now_ts is not None else _time.time()
    date_str = date_str or utc_date()
    hour = hour if hour is not None else utc_hour()
    tick_id = ts_iso or utc_now_iso()

    # roll day boundary + mark equity from current prices
    equity = state.mark_equity(prices, tick_id)
    state.roll_day(date_str, equity)
    state.tick_n += 1
    # pay-per-request premium signal via x402 every Nth tick (real micro-payment)
    if state.tick_n % max(1, cfg.get("x402", {}).get("every_n_ticks", 4)) == 0:
        _x402_signal(cfg, log)
    log.event("tick", tick_id=tick_id, equity=equity,
              drawdown=round(state.current_drawdown(equity), 4),
              dq_headroom=round(cfg["risk"]["drawdown_dq_reference_pct"]
                                - state.current_drawdown(equity), 4))

    # --- LAYERED DE-RISK (in order of severity) -----------------------------
    if state.halted:
        log.event("halted", note="kill switch tripped today; paused until next UTC day")
        return

    r = cfg["risk"]
    dd = state.current_drawdown(equity)

    # (3) Kill switch: peak-to-now drawdown at the kill line -> close all + pause
    #     for the rest of the UTC day (roll_day re-arms it next day; NOT a
    #     permanent halt — forfeiting a whole week on one intraday dip is the
    #     worst move in a rank-by-return contest where the DQ line is 30%).
    if dd >= r["drawdown_kill_pct"]:
        log.event("kill_switch", drawdown=round(dd, 4), positions=list(state.positions),
                  note=f"dd>={r['drawdown_kill_pct']} -> liquidate all + pause for the day")
        for tkn in list(state.positions):
            _force_close(executor, state, cfg, tick_id, tkn, _mark_px(prices, state, tkn),
                         log, "kill switch", now=now_ts)
        state.halted = True
        state.mark_equity(prices, tick_id)
        state.save(cfg["paths"]["state_file"])
        return

    # (1) Per-position stop: cut individual losers before they grow the drawdown.
    #     Only act on tokens with a REAL live price this tick — a missing/zero
    #     price must never be read as a -100% move and trigger a false stop.
    for tkn in list(state.positions):
        px = prices.get(tkn, 0.0)
        if px <= 0:
            continue
        pnl = state.position_pnl_pct(tkn, px)
        if pnl <= -r["per_position_stop_pct"]:
            log.event("position_stop", token=tkn, pnl_pct=round(pnl, 4),
                      note=f"<= -{r['per_position_stop_pct']} -> close")
            _force_close(executor, state, cfg, tick_id, tkn, px,
                         log, f"per-position stop ({pnl:.3f})", now=now_ts)

    # refresh equity after any stops
    equity = state.mark_equity(prices, tick_id)

    signals = score_universe(snapshot, cfg)
    for t, s in signals.items():
        log.event("signal", token=t, score=s.score, regime=s.regime.value,
                  actionable=s.actionable, components=s.components)

    portfolio = {
        "cash_usd": state.cash_usd,
        "total_equity_usd": equity,
        "positions": {t: p.qty for t, p in state.positions.items()},
    }
    risk_limits = {
        "max_position_pct": cfg["risk"]["max_position_pct"],
        "daily_loss_remaining_pct": max(0.0, cfg["risk"]["daily_loss_stop_pct"]
                                        - state.daily_loss(equity)),
        "max_trades_left_today": cfg["risk"]["max_trades_per_day"] - state.trades_today,
    }

    decisions = decider.decide(snapshot, signals, portfolio, risk_limits)
    tradeable = set(cfg["twak"]["token_contracts"])      # eligible + has a contract

    for d in decisions:
        token = d["token"]
        log.event("decision", **d)
        # A hold is a deliberate no-op, NOT a blocked trade — don't log it as one.
        if d["action"] == "hold":
            continue
        # Off-universe guard: only opening trades in tradeable tokens count.
        # (BTC/BNB are signal-only; their "buy" decisions are ignored here.)
        if d["action"] == "buy" and token not in tradeable:
            log.event("blocked", token=token, action="buy", reason="not_tradeable: off-universe")
            continue
        token_risk = snapshot.get(token, {}).get("token_risk_score", 0)  # TWAK fills live
        verdict = risk_gate.evaluate(
            token=token, action=d["action"], requested_size_pct=d["size_pct"],
            confidence=d["confidence"], token_risk_score=token_risk,
            state=state, equity=equity, cfg=cfg, now=now_ts,
        )
        if not verdict.approved:
            log.event("blocked", token=token, action=d["action"], reason=verdict.reason)
            continue

        _exec_and_log(executor, state, cfg, tick_id, token, d["action"],
                      verdict.adjusted_size_usd, prices.get(token, 0.0),
                      log, verdict.reason, now=now_ts)

    # --- Guarantee the min-1-trade/day requirement during the UTC day -------
    # Off-list/zero-trade days don't count. Trigger from 18:00 UTC (~24 retry
    # ticks before midnight, not a single 2h band) and try several candidates so
    # one blocked/illiquid name can't burn the day. If we can't BUY (low cash /
    # daily pause), trim a position instead — a close also counts as a trade and
    # needs no cash.
    if (r.get("force_daily_trade") and state.trades_today == 0
            and not state.halted and hour >= 18):
        cands = sorted((s for s in signals.values() if s.token in tradeable),
                       key=lambda s: abs(s.score), reverse=True)
        done = False
        if state.cash_usd > r["min_portfolio_usd"]:
            for s in cands[:5]:                     # try the 5 strongest in turn
                px = prices.get(s.token, 0.0)
                if px <= 0:
                    continue
                verdict = risk_gate.evaluate(
                    token=s.token, action="buy", requested_size_pct=0.05, confidence=0.6,
                    token_risk_score=snapshot.get(s.token, {}).get("token_risk_score", 0),
                    state=state, equity=equity, cfg=cfg, now=now_ts,
                )
                if verdict.approved:
                    _exec_and_log(executor, state, cfg, tick_id, s.token, "buy",
                                  verdict.adjusted_size_usd, px,
                                  log, "maintenance trade (min 1/day)", now=now_ts)
                    done = True
                    break
        if not done and state.positions:           # fall back: trim smallest holding
            tkn = min(state.positions,
                      key=lambda t: abs(state.positions[t].qty * state.positions[t].avg_price))
            _force_close(executor, state, cfg, tick_id, tkn, _mark_px(prices, state, tkn),
                         log, "maintenance trade (min 1/day, trim)", now=now_ts)
            done = True
        if not done:
            log.event("maintenance_skipped", reason="no deployable cash and no positions to trim")

    # final equity mark + persist
    state.mark_equity(prices, tick_id)
    state.save(cfg["paths"]["state_file"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--seed-cash", type=float, default=None,
                    help="initialize cash (first run only)")
    ap.add_argument("--list-cmc-tools", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    # Attribute every logged action to the agent's ERC-8004 on-chain identity.
    agent_id = str(cfg.get("bnb_sdk", {}).get("agent_id") or "") or None
    log = DecisionLog(cfg["paths"]["decision_log"], agent_id=agent_id)
    if agent_id:
        log.event("identity", erc8004_agent_id=agent_id,
                  agent_address=cfg.get("twak", {}).get("agent_address"),
                  note="ERC-8004 on-chain identity; all actions below are attributed to it")

    if args.list_cmc_tools:
        cmc = build_cmc_client({**cfg, "mode": "live"})
        for t in cmc.list_tools():
            print(t.get("name"), "—", t.get("description", "")[:80])
        return 0

    state = PortfolioState.load(cfg["paths"]["state_file"])
    if args.seed_cash is not None and state.initial_equity == 0:
        state.cash_usd = args.seed_cash
        state.initial_equity = args.seed_cash
        state.peak_equity = args.seed_cash
        state.day_start_equity = args.seed_cash
        log.event("seed", cash=args.seed_cash)

    reconcile(state, log)
    cmc = build_signal_source(cfg)
    decider = build_decider(cfg)
    executor = build_executor(cfg)

    interval = cfg["poll_interval_minutes"] * 60
    backoff = cfg["rpc"]["backoff_base_seconds"]

    def tick():
        run_tick(cfg, state, cmc, decider, executor, log)

    if args.once:
        tick()
        return 0

    while True:
        try:
            tick()
            backoff = cfg["rpc"]["backoff_base_seconds"]   # reset on success
            time.sleep(interval)
        except KeyboardInterrupt:
            log.event("shutdown", reason="keyboard interrupt")
            return 0
        except Exception as e:
            log.event("loop_error", error=str(e), backoff=backoff)
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    sys.exit(main())
