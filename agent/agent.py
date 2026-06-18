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
from .decision import build_decider
from .executor import build_executor
from .logbook import DecisionLog, utc_date, utc_now_iso
from .signal_engine import score_universe
from .state import PortfolioState


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


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


def run_tick(cfg, state, cmc, decider, executor, log) -> None:
    tick_id = utc_now_iso()
    snapshot = cmc.get_snapshot(cfg["whitelist"])
    prices = {t: d.get("price", 0.0) for t, d in snapshot.items()}

    # roll day boundary + mark equity from current prices
    equity = state.mark_equity(prices, tick_id)
    state.roll_day(utc_date(), equity)
    log.event("tick", tick_id=tick_id, equity=equity,
              drawdown=round(state.current_drawdown(equity), 4),
              dq_headroom=round(cfg["risk"]["drawdown_dq_reference_pct"]
                                - state.current_drawdown(equity), 4))

    # --- DE-RISK: if drawdown breaches the hard stop, force-close everything.
    # Going close-only isn't enough — open positions keep marking toward the DQ
    # line. Liquidating is what actually protects us from disqualification.
    dd = state.current_drawdown(equity)
    if dd >= cfg["risk"]["drawdown_hard_stop_pct"] and state.positions:
        log.event("derisk", drawdown=round(dd, 4), positions=list(state.positions),
                  note="hard stop breached -> force-closing all positions")
        for tkn in list(state.positions):
            state.save(cfg["paths"]["state_file"])
            try:
                tx = executor.execute(tick_id=tick_id, token=tkn, action="close",
                                      size_usd=0.0, price=prices.get(tkn, 0.0),
                                      state=state, log=log)
                log.event("fill", token=tkn, action="close", size_usd=0.0,
                          tx_hash=tx, reason="forced de-risk on hard stop")
            except Exception as e:
                log.event("exec_error", token=tkn, action="close", error=str(e))
            finally:
                state.save(cfg["paths"]["state_file"])
        state.mark_equity(prices, utc_now_iso())
        state.save(cfg["paths"]["state_file"])
        return   # skip new entries this tick

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

    for d in decisions:
        token = d["token"]
        log.event("decision", **d)
        token_risk = snapshot.get(token, {}).get("token_risk_score", 0)  # TWAK fills live
        verdict = risk_gate.evaluate(
            token=token, action=d["action"], requested_size_pct=d["size_pct"],
            confidence=d["confidence"], token_risk_score=token_risk,
            state=state, equity=equity, cfg=cfg,
        )
        if not verdict.approved:
            log.event("blocked", token=token, action=d["action"], reason=verdict.reason)
            continue
        if d["action"] in ("hold",):
            continue

        state.save(cfg["paths"]["state_file"])      # PERSIST-BEFORE-SEND (idempotency)
        try:
            tx = executor.execute(
                tick_id=tick_id, token=token, action=d["action"],
                size_usd=verdict.adjusted_size_usd, price=prices.get(token, 0.0),
                state=state, log=log,
            )
            log.event("fill", token=token, action=d["action"],
                      size_usd=verdict.adjusted_size_usd, tx_hash=tx,
                      reason=verdict.reason)
        except Exception as e:
            log.event("exec_error", token=token, action=d["action"], error=str(e))
        finally:
            state.save(cfg["paths"]["state_file"])

    # final equity mark + persist
    state.mark_equity(prices, utc_now_iso())
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
    log = DecisionLog(cfg["paths"]["decision_log"])

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
    cmc = build_cmc_client(cfg)
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
