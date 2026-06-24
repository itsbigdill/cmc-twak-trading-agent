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
from .decision import build_decider, runtime_validated_token, tradeable_buy_tokens
from .executor import AmbiguousExecutionError, build_executor
from .logbook import DecisionLog, utc_date, utc_hour, utc_now_iso
from .leaderboard import current_status
from .signal_engine import score_universe
from .state import PortfolioState, make_order_id


def _trace_entry_filter_reason(cfg: dict | None, token: str, signal,
                               snapshot: dict | None) -> str | None:
    """Explain why a validated candidate is or is not fresh enough to enter.

    This mirrors the deterministic entry filter at a diagnostic level so the
    dashboard/raw trace can answer "why no buy?" without re-implementing logic.
    It intentionally avoids ranking quality because trace generation should stay
    cheap and side-effect free; the concrete threshold reasons are still useful.
    """
    if not cfg or signal is None:
        return None
    if getattr(getattr(signal, "regime", None), "value", None) != "trend_down":
        return "not_downtrend_entry_filter"
    entry = cfg.get("decision", {}).get("entry_filter", {}) or {}
    if not entry.get("enabled", True):
        return "entry_filter_disabled"
    d = (snapshot or {}).get(token, {})
    score = float(getattr(signal, "score", 0.0) or 0.0)
    r6 = float(d.get("return_6h", 0.0) or 0.0)
    c1 = float(d.get("cmc_pct_1h", 0.0) or 0.0)
    c24 = float(d.get("cmc_pct_24h", 0.0) or 0.0)
    c7 = float(d.get("cmc_pct_7d", 0.0) or 0.0)
    rt = float(d.get("round_trip_loss_pct", 999.0) or 999.0)
    risk = float(d.get("token_risk_score", 100.0) or 100.0)
    x402 = float(d.get("x402_token_score", 0.0) or 0.0)
    cmc = float(d.get("cmc_score", 0.0) or 0.0)
    if entry.get("pullback_exception_enabled", False):
        if (
            score >= float(entry.get("pullback_min_score", 0.32))
            and float(entry.get("pullback_min_cmc_pct_1h_downtrend", -0.12))
            <= c1 <= float(entry.get("pullback_max_cmc_pct_1h_downtrend", -0.03))
            and float(entry.get("pullback_min_return_6h_downtrend", -0.02))
            <= r6 <= float(entry.get("pullback_max_return_6h_downtrend", 0.04))
            and c24 <= float(entry.get("pullback_max_cmc_pct_24h_downtrend", 0.24))
            and c7 <= float(entry.get("pullback_max_cmc_pct_7d_downtrend", 0.55))
            and x402 >= float(entry.get("pullback_min_x402", 0.25))
            and cmc >= float(entry.get("pullback_min_cmc", 0.80))
            and rt <= float(entry.get("pullback_max_round_trip_loss_pct", 2.0))
            and risk <= float(entry.get("pullback_max_token_risk_score", 30.0))
        ):
            return "validated_pullback_candidate"
    min_r6 = float(entry.get("min_return_6h_downtrend", 0.0))
    max_r6 = float(entry.get("max_return_6h_downtrend", 0.08))
    if r6 < min_r6 or r6 > max_r6:
        return f"bad_6h:{r6:.3f} not in [{min_r6:.3f},{max_r6:.3f}]"
    max_c1 = float(entry.get("max_cmc_pct_1h_downtrend", 0.03))
    if c1 > max_c1:
        return f"late_hot_1h:{c1:.3f}>{max_c1:.3f}"
    max_c24 = float(entry.get("max_cmc_pct_24h_downtrend", 0.18))
    if c24 > max_c24:
        return f"late_hot_24h:{c24:.3f}>{max_c24:.3f}"
    max_c7 = float(entry.get("max_cmc_pct_7d_downtrend", 0.45))
    if c7 > max_c7:
        return f"late_hot_7d:{c7:.3f}>{max_c7:.3f}"
    return "entry_filter_passed"


def _trace_token(token: str | None, signal=None, snapshot: dict | None = None,
                 *, tradeable: set[str] | None = None,
                 cfg: dict | None = None) -> dict | None:
    """Compact, machine-readable token diagnostics for decision_trace.

    This is intentionally generated inside the agent loop, not in the dashboard.
    The dashboard may display it, but it must never invent it.
    """
    if not token:
        return None
    d = (snapshot or {}).get(token, {}) if snapshot else {}
    meta = (cfg.get("universe_runtime", {}) if cfg else {}).get(token, {}) or {}
    max_rt = float((cfg or {}).get("execution", {}).get(
        "max_round_trip_loss_pct",
        (cfg or {}).get("universe", {}).get("max_round_trip_loss_pct", 3.0),
    ))
    try:
        rt_loss = float(d.get("round_trip_loss_pct", meta.get("round_trip_loss_pct", 999.0)))
    except (TypeError, ValueError):
        rt_loss = 999.0
    risk_level = str(d.get("risk_level", meta.get("risk_level", "")) or "").lower()
    history_bars = int(d.get("history_bars", meta.get("history_bars", 0)) or 0)
    validated = bool(rt_loss <= max_rt and risk_level != "high" and history_bars > 0)
    out = {
        "token": token,
        "score": getattr(signal, "score", None),
        "tradeable": token in (tradeable or set()),
        "validated": validated,
        "round_trip_loss_pct": round(rt_loss, 4) if rt_loss < 999 else None,
        "risk_level": risk_level or None,
        "history_bars": history_bars or None,
    }
    if signal is not None:
        out["components"] = getattr(signal, "components", {})
        out["actionable"] = getattr(signal, "actionable", None)
        out["entry_filter_reason"] = _trace_entry_filter_reason(cfg, token, signal, snapshot)
    for k in ("x402_token_score", "cmc_score", "cmc_rank", "cmc_volume_24h",
              "cmc_pct_1h", "cmc_pct_24h", "cmc_pct_7d",
              "token_risk_score", "return_6h", "return_24h"):
        if k in d:
            out[k] = d.get(k)
    return out


def _decision_trace(cfg, tick_id, snapshot, signals, portfolio, risk_limits,
                    decisions, tradeable, *, risk_outcomes=None) -> dict:
    """Build the internal causal trace for a tick.

    The trace answers "why no trade?" and "why this trade?" using the same
    config/signal objects the agent used. It is logged as JSONL for audit and
    debugging; UI code should only read this artifact, not re-create reasoning.
    """
    ranked = sorted(signals.values(), key=lambda s: s.score, reverse=True)
    trade_ranked = [s for s in ranked if s.token in tradeable]

    def _validated(s):
        t = _trace_token(s.token, s, snapshot, tradeable=tradeable, cfg=cfg)
        return bool(t and t.get("validated"))

    valid_ranked = [s for s in trade_ranked if _validated(s)]
    regime = ranked[0].regime.value if ranked else None
    if regime == "trend_down":
        threshold_name = "rotation_downtrend_min_momentum"
        threshold = float(cfg.get("decision", {}).get(threshold_name, 0.0))
    elif regime == "trend_up":
        threshold_name = "rotation_min_momentum"
        threshold = float(cfg.get("decision", {}).get(threshold_name, 0.0))
    elif regime == "chop":
        threshold_name = "chop_no_new_entries"
        threshold = None
    else:
        threshold_name = "no_signals"
        threshold = None

    best_for_gate = valid_ranked[0] if valid_ranked else (trade_ranked[0] if trade_ranked else None)
    best_for_gate_trace = (_trace_token(best_for_gate.token, best_for_gate, snapshot,
                                        tradeable=tradeable, cfg=cfg)
                           if best_for_gate else None)
    entry_filter_reason = (best_for_gate_trace or {}).get("entry_filter_reason")
    confirm = cfg.get("decision", {}).get("signal_confirmation", {})
    immediate = float(confirm.get("immediate_score", 0.48))
    required_ticks = int(confirm.get("required_ticks", 3))
    streaks = risk_limits.get("signal_streaks", {})
    streak = int(streaks.get(best_for_gate.token, 0)) if best_for_gate else 0
    passed_threshold = (best_for_gate is not None and threshold is not None
                        and best_for_gate.score > threshold)
    confirmed = bool(best_for_gate and (best_for_gate.score >= immediate
                     or streak >= required_ticks))

    if decisions:
        final_action = ",".join(f"{d.get('action')}:{d.get('token')}" for d in decisions)
        reason = "candidate_decisions_emitted"
    elif regime == "chop":
        final_action, reason = "hold", "chop_regime_no_new_entries"
    elif not ranked:
        final_action, reason = "hold", "no_signals"
    elif not trade_ranked:
        final_action, reason = "hold", "no_tradeable_signal"
    elif not valid_ranked:
        final_action, reason = "hold", "no_runtime_validated_candidate"
    elif not passed_threshold:
        final_action, reason = "hold", f"best_validated_score_below_{threshold_name}"
    elif not confirmed:
        final_action, reason = "hold", "signal_confirmation_streak_not_met"
    elif entry_filter_reason and entry_filter_reason not in {
        "entry_filter_passed",
        "validated_pullback_candidate",
        "entry_filter_disabled",
        "not_downtrend_entry_filter",
    }:
        final_action, reason = "hold", f"entry_filter_rejected:{entry_filter_reason}"
    else:
        # If we got here, the deterministic candidate was likely removed by an
        # upstream hysteresis/rotation rule or the LLM second gate.
        final_action, reason = "hold", "candidate_suppressed_by_rotation_or_llm_gate"

    return {
        "kind": "decision_trace",
        "tick_id": tick_id,
        "strategy": cfg.get("decision", {}).get("strategy_label"),
        "regime": regime,
        "portfolio": {
            "cash_usd": round(float(portfolio.get("cash_usd", 0.0)), 6),
            "total_equity_usd": round(float(portfolio.get("total_equity_usd", 0.0)), 6),
            "positions": portfolio.get("positions", {}),
        },
        "gate": {
            "name": threshold_name,
            "required": threshold,
            "actual": best_for_gate.score if best_for_gate else None,
            "passed_threshold": passed_threshold,
            "streak": streak,
            "required_ticks": required_ticks,
            "confirmed": confirmed,
        },
        "best_signal": _trace_token(ranked[0].token, ranked[0], snapshot,
                                    tradeable=tradeable, cfg=cfg) if ranked else None,
        "best_tradeable": _trace_token(trade_ranked[0].token, trade_ranked[0], snapshot,
                                       tradeable=tradeable, cfg=cfg) if trade_ranked else None,
        "best_validated": _trace_token(valid_ranked[0].token, valid_ranked[0], snapshot,
                                       tradeable=tradeable, cfg=cfg) if valid_ranked else None,
        "candidate_decisions": decisions,
        "risk_outcomes": risk_outcomes or [],
        "final_action": final_action,
        "reason": reason,
    }


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["_config_dir"] = os.path.dirname(os.path.abspath(path))
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
    # Preserve the curated base, then overlay the last execution-validated
    # dynamic universe.  Readers such as the quick dashboard builder should see
    # the same universe as the live agent even when they do not instantiate the
    # signal source/UniverseManager.
    cfg["_static_contracts"] = dict(cfg["twak"]["token_contracts"])
    if cfg.get("universe", {}).get("enabled"):
        import json as _json
        cache = cfg["universe"].get("cache_file", "state/universe_cache.json")
        cp = cache if os.path.isabs(cache) else os.path.join(cfg["_config_dir"], cache)
        try:
            cached = _json.load(open(cp))
            cfg["twak"]["token_contracts"].update(cached.get("assets") or {})
            cfg["universe_runtime"] = cached.get("metrics") or {}
        except Exception:
            pass
    # Signal universe MUST equal the trade universe, else the strategy can only
    # act on tokens it also has signals for. Derive it from token_contracts so
    # the two can never drift (benchmark drives regime; quote is the cash leg).
    bench = cfg["regime"]["benchmark"]
    trade = list(cfg["twak"]["token_contracts"])
    cfg["whitelist"] = [bench] + [t for t in trade if t != bench] + [cfg["quote_asset"]]
    # Mode is overridable by env so a server can run paper/live without editing
    # (and conflicting with) the repo's config.yaml on git pull.
    cfg["mode"] = os.environ.get("AGENT_MODE", cfg.get("mode", "dry_run"))
    # Safety rail: a local/diagnostic dry-run must never mutate the live state
    # files.  This protects production from accidental `python -m agent.agent
    # --once` invocations without AGENT_MODE=live (mock fills would otherwise
    # contaminate the real dashboard/state).
    if cfg["mode"] != "live" and cfg.get("safety", {}).get("isolate_non_live_state", True):
        mode = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in cfg["mode"])
        paths = cfg.setdefault("paths", {})
        if paths.get("state_file") == "state/portfolio.json":
            paths["state_file"] = f"state/{mode}_portfolio.json"
        if paths.get("decision_log") == "logs/decisions.jsonl":
            paths["decision_log"] = f"logs/{mode}_decisions.jsonl"
    return cfg


def reconcile(state: PortfolioState, log: DecisionLog, executor=None) -> None:
    """Resolve interrupted orders from persisted pre-trade balance snapshots."""
    pend = state.pending_orders()
    for o in pend:
        try:
            result = executor.reconcile(o, state) if hasattr(executor, "reconcile") else None
            if result and result.filled:
                log.event("reconcile_fill", order_id=o.client_order_id, token=o.token,
                          action=o.action, tx_hash=result.tx_hash,
                          fill_price=result.fill_price, quantity=result.quantity,
                          quote_amount=result.quote_amount)
                continue
            if o.status == "FAILED":
                log.event("reconcile_failed", order_id=o.client_order_id, token=o.token,
                          action=o.action, tx_hash=o.tx_hash, error=o.error)
                continue
            if o.pre_token_atomic is None or o.pre_quote_atomic is None:
                state.reconcile_order(o.client_order_id,
                                      "legacy unresolved order; no balance snapshots")
            else:
                state.fail_order(o.client_order_id,
                                 "no conclusive balance delta; manual review required",
                                 unknown=True)
            log.event("reconcile", order_id=o.client_order_id, token=o.token,
                      action=o.action, status=o.status, tx_hash=o.tx_hash,
                      note=o.error or "not resent")
        except Exception as e:
            state.fail_order(o.client_order_id, f"reconciliation failed: {e}", unknown=True)
            log.event("reconcile_error", order_id=o.client_order_id, token=o.token,
                      action=o.action, error=str(e))


def _x402_signal(cfg, log, state) -> None:
    """Pay-per-request for a premium market signal via TWAK x402 (CDP facilitator).
    Real on-chain micro-payment as part of the trade loop (Best-TWAK rubric). The paid
    signal's `bias` is stored on state and fed into scoring next ticks (load-bearing).
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
        # the premium signal is the resource response body (twak nests it under "data")
        sig = data.get("data") if isinstance(data.get("data"), dict) else data
        bias = sig.get("bias") if isinstance(sig, dict) else None
        if bias is not None:
            try:
                state.x402_bias = max(-1.0, min(1.0, float(bias)))   # <- now affects scoring
            except (TypeError, ValueError):
                pass
        # Token-level x402 signals are more useful than a global market bias in
        # this tournament: the paid endpoint returns a ranked "top" list, so feed
        # those scores into each token's deterministic score.  Missing tokens get
        # 0 downstream; malformed rows are ignored.
        boosts: dict[str, float] = {}
        if isinstance(sig, dict):
            for row in sig.get("top") or []:
                if not isinstance(row, dict):
                    continue
                token = str(row.get("token") or "").upper()
                if not token:
                    continue
                try:
                    boosts[token] = max(-1.0, min(1.0, float(row.get("score", 0.0))))
                except (TypeError, ValueError):
                    continue
        if boosts:
            state.x402_tokens = boosts
        log.event("x402", url=url, tx=tx, paid=data.get("amountPaid") or cap,
                  bias=state.x402_bias, token_boosts=boosts, signal=sig)
    except Exception as e:
        log.event("x402_error", error=str(e))


def _erc8004_attest(cfg, state, log) -> None:
    """Write the agent's live track record on-chain to its ERC-8004 identity metadata
    (Best-BNB-SDK: the identity becomes load-bearing — an on-chain, verifiable reputation
    record, not just a minted NFT). No-op without an agent_id; graceful on error."""
    import json
    import re
    import subprocess
    aid = str(cfg.get("bnb_sdk", {}).get("agent_id") or "")
    if not aid:
        return
    eq = state.equity_curve[-1][1] if state.equity_curve else (state.initial_equity or 0.0)
    ret = (eq / state.initial_equity - 1) * 100 if state.initial_equity else 0.0
    perf = json.dumps({"equity": round(eq, 2), "return_pct": round(ret, 2),
                       "trades": state.trade_count_total,
                       "dd_pct": round(state.current_drawdown(eq) * 100, 2)})
    try:
        out = subprocess.run(["twak", "erc8004", "set-metadata", aid, "--key", "cta-perf",
                              "--value", perf, "--chain", "bsc", "--json"],
                             capture_output=True, text=True, timeout=120)
        m = re.search(r"0x[0-9a-fA-F]{64}", (out.stdout or "") + (out.stderr or ""))
        log.event("erc8004_attest", agent_id=aid, perf=perf, tx=m.group(0) if m else None)
    except Exception as e:
        log.event("erc8004_error", error=str(e))


def _exec_and_log(executor, state, cfg, tick_id, token, action, size_usd, price,
                  log, reason, now=None) -> bool:
    """Execute once, logging/counting only a balance-reconciled confirmed fill."""
    now_ts = now if now is not None else time.time()
    oid = make_order_id(tick_id, token, action)
    retry_at = state.execution_retry_after.get(token, 0.0)
    if now_ts < retry_at:
        log.event("execution_backoff", token=token, action=action,
                  retry_after=retry_at, seconds_remaining=round(retry_at - now_ts, 1))
        return False
    if state.has_unresolved_order(token):
        log.event("execution_blocked", token=token, action=action,
                  reason="unresolved prior order requires reconciliation")
        return False

    before = state.realized_pnl
    persist = lambda: state.save(cfg["paths"]["state_file"])
    ex_cfg = cfg.get("execution", {})
    try:
        result = executor.execute(tick_id=tick_id, token=token, action=action,
                                  size_usd=size_usd, price=price, state=state, log=log,
                                  now=now_ts, persist=persist)
        if not result.filled:
            log.event("execution_noop", token=token, action=action,
                      order_id=result.order_id, status=result.status, note=result.note)
            return False
        # realized P&L booked by this trade (closes/sells); ~0 for opens
        realized = round(state.realized_pnl - before, 4)
        state.clear_execution_failure(token)
        log.event("fill", token=token, action=action,
                  size_usd=round(result.quote_amount, 8), tx_hash=result.tx_hash,
                  reason=reason, realized=realized,
                  fill_price=round(result.fill_price, 10), quantity=result.quantity,
                  order_id=result.order_id)
        return True
    except AmbiguousExecutionError as e:
        if e.tx_hash and oid in state.open_orders:
            state.mark_sent(oid, e.tx_hash)
        state.fail_order(oid, str(e), unknown=True)
        retry = state.record_execution_failure(
            token, now_ts, float(ex_cfg.get("retry_base_seconds", 60)),
            float(ex_cfg.get("retry_max_seconds", 900)),
        )
        log.event("exec_unknown", token=token, action=action, order_id=oid,
                  tx_hash=e.tx_hash or None, error=str(e), retry_after=retry)
        return False
    except Exception as e:
        state.fail_order(oid, str(e))
        retry = state.record_execution_failure(
            token, now_ts, float(ex_cfg.get("retry_base_seconds", 60)),
            float(ex_cfg.get("retry_max_seconds", 900)),
        )
        log.event("exec_error", token=token, action=action, order_id=oid,
                  error=str(e), retry_after=retry)
        return False
    finally:
        state.save(cfg["paths"]["state_file"])


def _force_close(executor, state, cfg, tick_id, token, price, log, reason, now=None) -> bool:
    return _exec_and_log(executor, state, cfg, tick_id, token, "close", 0.0,
                         price, log, reason, now=now)


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
    if state.pending_orders():
        reconcile(state, log, executor)
        state.save(cfg["paths"]["state_file"])
    if hasattr(cmc, "maybe_refresh_universe"):
        try:
            if cmc.maybe_refresh_universe():
                log.event("universe_refreshed", assets=len(cfg["twak"]["token_contracts"]))
        except Exception as e:
            log.event("universe_refresh_error", error=str(e))
    snapshot = cmc.get_snapshot(cfg["whitelist"])
    prices = {t: d.get("price", 0.0) for t, d in snapshot.items()}
    # Risk marks and stops use what the position can actually be liquidated for,
    # not a potentially stale CMC mark.  Signal calculation still uses CMC/TWAK
    # history, keeping market selection independent from execution mechanics.
    if cfg.get("mode") == "live" and hasattr(executor, "executable_price"):
        for token in list(state.positions):
            try:
                px = executor.executable_price(token)
                if px > 0:
                    prices[token] = px
                    snapshot.setdefault(token, {})["executable_price"] = px
                    log.event("liquidation_quote", token=token, executable_price=px,
                              reference_price=snapshot[token].get("price"))
                else:
                    snapshot.setdefault(token, {})["executable_quote_failed"] = True
            except Exception as e:
                snapshot.setdefault(token, {})["executable_quote_failed"] = True
                log.event("liquidation_quote_error", token=token, error=str(e))
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
        _x402_signal(cfg, log, state)
    # attest the agent's track record on-chain to its ERC-8004 identity (~daily)
    if cfg.get("mode") in ("live", "paper") and \
       state.tick_n % max(1, cfg.get("bnb_sdk", {}).get("attest_every_n_ticks", 96)) == 0:
        _erc8004_attest(cfg, state, log)
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
        # In live mode, never infer a stop from a non-executable reference mark.
        if snapshot.get(tkn, {}).get("executable_quote_failed"):
            continue
        px = snapshot.get(tkn, {}).get("executable_price", prices.get(tkn, 0.0))
        if px <= 0:
            continue
        pos = state.positions[tkn]
        pos.peak_executable_price = max(pos.peak_executable_price or pos.avg_price, px)
        pnl = state.position_pnl_pct(tkn, px)
        lock = cfg.get("profit_lock", {})
        stop_reason = None
        stop_price = None
        if pnl <= -r["per_position_stop_pct"]:
            stop_reason = f"per-position stop ({pnl:.3f})"
        elif lock.get("enabled") and pos.avg_price > 0:
            peak_pnl = pos.peak_executable_price / pos.avg_price - 1.0
            if peak_pnl >= float(lock.get("activation_pct", 0.10)):
                stop_price = pos.avg_price * (1.0 + float(lock.get("breakeven_floor_pct", 0.02)))
                if peak_pnl >= float(lock.get("trailing_activation_pct", 0.20)):
                    stop_price = max(stop_price, pos.peak_executable_price
                                     * (1.0 - float(lock.get("trailing_gap_pct", 0.08))))
                if px <= stop_price:
                    stop_reason = f"profit lock (peak={peak_pnl:.3f}, floor={stop_price:.8f})"
        if stop_reason:
            log.event("position_stop", token=tkn, pnl_pct=round(pnl, 4),
                      executable_price=px, stop_price=stop_price, note=stop_reason)
            _force_close(executor, state, cfg, tick_id, tkn, px,
                         log, stop_reason, now=now_ts)
            continue
        if lock.get("enabled") and not pos.profit_taken \
                and pnl >= float(lock.get("partial_take_pct", 0.25)):
            trim_usd = pos.qty * px * float(lock.get("partial_sell_fraction", 0.25))
            if trim_usd >= float(lock.get("min_partial_sell_usd", 1.0)):
                done = _exec_and_log(executor, state, cfg, tick_id, tkn, "trim",
                                     trim_usd, px, log,
                                     f"partial profit take ({pnl:.3f})", now=now_ts)
                if done and tkn in state.positions:
                    state.positions[tkn].profit_taken = True
                    state.save(cfg["paths"]["state_file"])

    # refresh equity after any stops
    equity = state.mark_equity(prices, tick_id)

    # Premium x402 signal (bought per-request) is load-bearing:
    #   * global bias tilts the whole market (if the endpoint sends it)
    #   * token boosts tilt only the paid endpoint's ranked top names
    for d in snapshot.values():
        d["x402_bias"] = state.x402_bias
    for token, boost in state.x402_tokens.items():
        if token in snapshot:
            snapshot[token]["x402_token_score"] = boost
    signals = score_universe(snapshot, cfg)
    confirm = cfg.get("decision", {}).get("signal_confirmation", {})
    base_confirm = float(confirm.get("base_score", cfg["signal"]["entry_threshold"]))
    for token, signal in signals.items():
        state.signal_streaks[token] = (state.signal_streaks.get(token, 0) + 1
                                       if signal.score >= base_confirm else 0)
    for t, s in signals.items():
        log.event("signal", token=t, score=s.score, regime=s.regime.value,
                  actionable=s.actionable, components=s.components)

    portfolio = {
        "cash_usd": state.cash_usd,
        "total_equity_usd": equity,
        "positions": {t: p.qty for t, p in state.positions.items()},
        "avg_prices": {t: p.avg_price for t, p in state.positions.items()},
        "position_opened_ts": {t: p.opened_ts for t, p in state.positions.items()},
        "position_values": {t: p.qty * prices.get(t, p.avg_price)
                            for t, p in state.positions.items()},
    }
    leaderboard = current_status(cfg) if cfg.get("mode") == "live" else {}
    executable_return_pct = ((equity / state.initial_equity - 1.0) * 100
                             if state.initial_equity > 0 else 0.0)
    risk_limits = {
        "max_position_pct": cfg["risk"]["max_position_pct"],
        "daily_loss_remaining_pct": max(0.0, cfg["risk"]["daily_loss_stop_pct"]
                                        - state.daily_loss(equity)),
        "max_trades_left_today": cfg["risk"]["max_trades_per_day"] - state.entries_today,
        "leaderboard_rank": leaderboard.get("rank"),
        "leaderboard_return_pct": leaderboard.get("return_pct"),
        "leaderboard_drawdown_pct": leaderboard.get("drawdown_pct"),
        "leaderboard_top5_return_pct": leaderboard.get("top5_return_pct"),
        "executable_return_pct": executable_return_pct,
        "current_drawdown_pct": state.current_drawdown(equity) * 100.0,
        "signal_streaks": dict(state.signal_streaks),
    }

    setattr(decider, "_now", now_ts)                     # for time-based re-entry cooldown
    decisions = decider.decide(snapshot, signals, portfolio, risk_limits)
    # deny_buy is entry-only and conditional: a quarantined token can buy again
    # only after UniverseManager records a successful executable round-trip.
    tradeable = tradeable_buy_tokens(cfg)
    risk_outcomes = []

    pending = []
    for d in decisions:
        token = d["token"]
        log.event("decision", **d)
        # A hold is a deliberate no-op, NOT a blocked trade — don't log it as one.
        if d["action"] == "hold":
            risk_outcomes.append({"token": token, "action": "hold", "approved": True,
                                  "reason": "explicit_hold"})
            continue
        # Off-universe guard: only opening trades in tradeable tokens count.
        # (BTC/BNB are signal-only; their "buy" decisions are ignored here.)
        if d["action"] == "buy" and token not in tradeable:
            log.event("blocked", token=token, action="buy", reason="not_tradeable: off-universe")
            risk_outcomes.append({"token": token, "action": "buy", "approved": False,
                                  "reason": "not_tradeable: off-universe"})
            continue
        if d["action"] == "buy" and not runtime_validated_token(cfg, token, snapshot):
            reason = "not_execution_validated: no fresh buy->sell round-trip"
            log.event("blocked", token=token, action="buy", reason=reason)
            risk_outcomes.append({"token": token, "action": "buy", "approved": False,
                                  "reason": reason})
            continue
        token_risk = snapshot.get(token, {}).get("token_risk_score", 0)  # TWAK fills live
        verdict = risk_gate.evaluate(
            token=token, action=d["action"], requested_size_pct=d["size_pct"],
            confidence=d["confidence"], token_risk_score=token_risk,
            state=state, equity=equity, cfg=cfg, now=now_ts,
            leaderboard_drawdown_pct=risk_limits.get("leaderboard_drawdown_pct"),
        )
        if not verdict.approved:
            log.event("blocked", token=token, action=d["action"], reason=verdict.reason)
            risk_outcomes.append({"token": token, "action": d["action"], "approved": False,
                                  "reason": verdict.reason})
            continue
        risk_outcomes.append({"token": token, "action": d["action"], "approved": True,
                              "reason": verdict.reason,
                              "adjusted_size_usd": round(verdict.adjusted_size_usd, 6)})
        pending.append((d, verdict))

    # Rotation is a two-leg intent: close the stale holding, then buy the new
    # target.  Risk checks must happen before either leg mutates state; otherwise
    # the close updates last_trade_ts and the buy is falsely blocked by the
    # anti-churn cooldown.  If the entry leg was not approved, do not close a
    # merely-rotated position into cash unless a separate stop/profit-lock forced
    # the exit earlier in this tick.
    approved_buys = [d for d, _ in pending if d.get("action") == "buy"]
    rotation_close_tokens = {
        d["token"] for d, _ in pending
        if d.get("action") == "close" and "rotate out" in str(d.get("rationale", ""))
    }
    if rotation_close_tokens and not approved_buys:
        pending = [
            (d, v) for d, v in pending
            if not (d.get("action") == "close" and d.get("token") in rotation_close_tokens)
        ]
        risk_outcomes = [
            r for r in risk_outcomes
            if not (r.get("approved") and r.get("action") == "close"
                    and r.get("token") in rotation_close_tokens)
        ]
        for token in sorted(rotation_close_tokens):
            reason = "paired_rotation_entry_not_approved"
            log.event("blocked", token=token, action="close", reason=reason)
            risk_outcomes.append({"token": token, "action": "close", "approved": False,
                                  "reason": reason})

    filled_rotation_closes = set()
    approved_rotation_closes = set(rotation_close_tokens)
    for d, verdict in pending:
        token = d["token"]
        execution_size = (float(d.get("size_usd", 0.0)) if d["action"] == "trim"
                          else verdict.adjusted_size_usd)
        if d["action"] == "buy" and approved_rotation_closes \
                and not filled_rotation_closes.issuperset(approved_rotation_closes):
            reason = "paired_rotation_close_not_filled"
            log.event("blocked", token=token, action="buy", reason=reason)
            risk_outcomes.append({"token": token, "action": "buy", "approved": False,
                                  "reason": reason})
            continue
        done = _exec_and_log(executor, state, cfg, tick_id, token, d["action"],
                             execution_size, prices.get(token, 0.0),
                             log, verdict.reason, now=now_ts)
        if done and d["action"] == "close" and token in approved_rotation_closes:
            filled_rotation_closes.add(token)

    log.write(_decision_trace(cfg, tick_id, snapshot, signals, portfolio, risk_limits,
                              decisions, tradeable, risk_outcomes=risk_outcomes))

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

    executor = build_executor(cfg)
    if cfg.get("mode") == "live" and hasattr(executor, "preflight"):
        executor.preflight()
    reconcile(state, log, executor)
    if cfg.get("mode") == "live" and hasattr(executor, "reconcile_portfolio"):
        changes = executor.reconcile_portfolio(state)
        if changes:
            log.event("portfolio_reconciled", changes=changes)
    state.save(cfg["paths"]["state_file"])
    cmc = build_signal_source(cfg)
    decider = build_decider(cfg)

    interval = cfg["poll_interval_minutes"] * 60
    backoff = cfg["rpc"]["backoff_base_seconds"]

    def tick():
        run_tick(cfg, state, cmc, decider, executor, log)

    if args.once:
        tick()
        return 0

    while True:
        try:
            tick_started = time.monotonic()
            tick()
            backoff = cfg["rpc"]["backoff_base_seconds"]   # reset on success
            # Fixed-rate scheduler: a "1 minute" loop should not become
            # tick_duration + 60s.  If a full-universe scan takes longer than
            # the interval, start the next tick immediately after it finishes;
            # never overlap ticks against the same portfolio state.
            elapsed = time.monotonic() - tick_started
            time.sleep(max(0.0, interval - elapsed))
        except KeyboardInterrupt:
            log.event("shutdown", reason="keyboard interrupt")
            return 0
        except Exception as e:
            log.event("loop_error", error=str(e), backoff=backoff)
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    sys.exit(main())
