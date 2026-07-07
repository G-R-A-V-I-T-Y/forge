"""store/ledger.py -- Git-native append-only data ledger.

Every historically-meaningful fact Forge produces (market data, decisions,
closed trades, account snapshots) is appended as one JSON line per record
to a monthly-partitioned file under `ledger/`, which is committed to git
(see store/git_sync.py) instead of living only in the gitignored
data/forge.db. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.

append_ledger_record() never raises -- a ledger write must never block or
crash the caller's primary operation (heartbeat, decision cycle, trade
close), mirroring market/heartbeat.py's append_historical().
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LEDGER_DIR = "ledger"


def _partition_path(kind: str, when: datetime, ledger_dir: str) -> str:
    month = when.strftime("%Y-%m")
    return os.path.join(ledger_dir, kind, f"{month}.jsonl")


def append_ledger_record(
    kind: str,
    record: dict,
    when: datetime | None = None,
    ledger_dir: str | None = None,
) -> None:
    """Append one record as a JSON line to ledger/{kind}/{YYYY-MM}.jsonl.

    `kind` is the ledger stream name (e.g. "decisions", "candles_5m",
    "trades", "accounts"). `when` determines the month partition; defaults
    to now (UTC). `ledger_dir` defaults to the CURRENT value of module-level
    LEDGER_DIR, read at call time rather than bound into the signature at
    def time -- Python evaluates default argument values once, at function
    definition, so `ledger_dir: str = LEDGER_DIR` would silently ignore any
    later `monkeypatch.setattr(store.ledger, "LEDGER_DIR", ...)` in tests
    (or any other runtime reassignment) for every caller that relies on the
    default. The `None`-sentinel pattern here is what lets tests redirect
    the ledger location without every call site needing to pass ledger_dir
    explicitly. Failure is silently swallowed and logged -- this path can
    never block or crash the caller.
    """
    try:
        moment = when or datetime.now(timezone.utc)
        effective_dir = ledger_dir if ledger_dir is not None else LEDGER_DIR
        path = _partition_path(kind, moment, effective_dir)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.warning("failed to append ledger record kind=%s", kind, exc_info=True)
