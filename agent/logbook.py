"""
Structured decision logging (F6) — the primary artifact for judges.

Every tick appends one JSON line capturing the full causal chain:
  market snapshot -> signal scores -> LLM reasoning -> risk decision -> tx hash.
This is what makes the agent reproducible and what we show judges to prove
rule adherence (including BLOCKED trades).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DecisionLog:
    def __init__(self, path: str):
        self.path = path

    def write(self, record: dict) -> None:
        record.setdefault("ts", utc_now_iso())
        with open(self.path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def event(self, kind: str, **fields) -> None:
        """Convenience: log a typed event (tick, signal, decision, block, fill, error)."""
        self.write({"kind": kind, **fields})
