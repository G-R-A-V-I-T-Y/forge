"""store/state_snapshot.py -- git-tracked current-state snapshot.

Unlike ledger/ (append-only history), state/current.json is overwritten
every cycle: it captures *right now* -- open positions, live balances,
agent status -- so a fresh `git clone` restores exactly where the desk
left off, not just its history. Small and bounded by agent count, so
committing it every cycle (see store/git_sync.py) is cheap. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = os.path.join("state", "current.json")


def build_current_state(conn) -> dict:
    from store.db import get_latest_account
    from store.positions import get_all_open_positions

    agents = [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, status, current_thesis_version, last_model_used FROM agents"
        ).fetchall()
    ]
    for agent in agents:
        paper = get_latest_account(conn, agent["id"], "paper")
        agent["paper_balance"] = paper["balance"] if paper else None
        agent["paper_peak"] = paper["peak_balance"] if paper else None
        live = get_latest_account(conn, agent["id"], "live")
        agent["live_balance"] = live["balance"] if live else None

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": agents,
        "open_positions": get_all_open_positions(conn),
    }


def write_current_state(conn, path: str = DEFAULT_STATE_PATH) -> None:
    """Atomically overwrite `path` with the current desk state. Best-effort
    -- must never block or crash the heartbeat cycle that calls it."""
    try:
        state = build_current_state(conn)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        logger.warning("failed to write current-state snapshot", exc_info=True)
