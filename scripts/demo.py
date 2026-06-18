"""
Offline demo runner — simulates a multi-tick session for judges.

Runs N ticks in-process with varying synthetic market data and simulated 15-min
spacing (so rate limits don't suppress trades). Produces a full decisions.jsonl
and equity curve, then you run `python -m agent.reporting`.

    python scripts/demo.py --ticks 40 --cash 200

This is the artifact to record in the demo video alongside the live agent.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import risk_gate
from agent.agent import load_config, run_tick
from agent.cmc_client import MockCMCClient
from agent.decision import RuleBasedDecider
from agent.executor import MockExecutor
from agent.logbook import DecisionLog
from agent.state import PortfolioState


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=40)
    ap.add_argument("--cash", type=float, default=200.0)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # fresh artifacts
    for p in (cfg["paths"]["state_file"], cfg["paths"]["decision_log"]):
        if os.path.exists(p):
            os.remove(p)

    log = DecisionLog(cfg["paths"]["decision_log"])
    state = PortfolioState(cash_usd=args.cash, initial_equity=args.cash,
                           peak_equity=args.cash, day_start_equity=args.cash)
    cmc = MockCMCClient(seed=7)          # reproducible demo
    decider = RuleBasedDecider(cfg)
    executor = MockExecutor(cfg)

    for i in range(args.ticks):
        # simulate 15-min spacing so min_seconds_between_trades passes
        state.last_trade_ts = 0
        run_tick(cfg, state, cmc, decider, executor, log)

    print(f"Ran {args.ticks} ticks. Now: python -m agent.reporting")


if __name__ == "__main__":
    main()
