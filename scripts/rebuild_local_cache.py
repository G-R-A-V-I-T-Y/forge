#!/usr/bin/env python
"""scripts/rebuild_local_cache.py -- Disaster-recovery rebuild of data/forge.db.

Reconstructs the local, gitignored SQLite cache purely from the git-tracked
ledger/ and state/ directories. This is the concrete proof of the "burned
laptop -> git pull -> back to normal" requirement: after cloning the repo
fresh, run this once before `python forge.py`. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from store.db import (
    get_connection,
    init_schema,
    insert_account_snapshot,
    insert_agent,
    insert_position,
    insert_trade,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "forge.db"
DEFAULT_LEDGER_DIR = PROJECT_ROOT / "ledger"
DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "current.json"


def _read_partitions(ledger_dir: Path, kind: str) -> pd.DataFrame:
    """Concatenate every .parquet and .jsonl partition for one ledger kind,
    oldest to newest. Empty DataFrame if the kind has no data yet."""
    kind_dir = ledger_dir / kind
    if not kind_dir.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(p) for p in sorted(kind_dir.glob("*.parquet"))]
    frames += [pd.read_json(p, lines=True) for p in sorted(kind_dir.glob("*.jsonl"))]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def rebuild(
    db_path: Path = DEFAULT_DB_PATH,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    state_path: Path = DEFAULT_STATE_PATH,
) -> dict:
    """Rebuild db_path from scratch using only the git-tracked ledger and
    state snapshot. Refuses to run against an existing db_path -- move it
    aside first if you really want to rebuild over it."""
    if db_path.exists():
        raise FileExistsError(
            f"{db_path} already exists -- refusing to overwrite. "
            "Move it aside first if you really want to rebuild."
        )

    state = json.loads(state_path.read_text(encoding="utf-8"))

    conn = get_connection(str(db_path))
    init_schema(conn)

    for agent in state["agents"]:
        insert_agent(conn, agent["id"], agent["name"], state["generated_at"], "{}")
        conn.execute(
            "UPDATE agents SET status = ?, current_thesis_version = ?, last_model_used = ? WHERE id = ?",
            (agent["status"], agent["current_thesis_version"], agent["last_model_used"], agent["id"]),
        )
    conn.commit()

    trades_df = _read_partitions(ledger_dir, "trades")
    for _, row in trades_df.iterrows():
        insert_trade(conn, row.dropna().to_dict())

    accounts_df = _read_partitions(ledger_dir, "accounts")
    for _, row in accounts_df.iterrows():
        insert_account_snapshot(conn, row["agent_id"], row["mode"], row["balance"], row["peak_balance"])

    for agent in state["agents"]:
        if agent.get("paper_balance") is None:
            insert_account_snapshot(conn, agent["id"], "paper", 50000.0, 50000.0)

    for position in state.get("open_positions", []):
        insert_position(conn, position)

    conn.close()

    return {
        "db_path": str(db_path),
        "agents": len(state["agents"]),
        "trades": len(trades_df),
        "accounts": len(accounts_df),
        "open_positions_in_state": len(state.get("open_positions", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild data/forge.db from the git ledger")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--ledger-dir", type=Path, default=DEFAULT_LEDGER_DIR)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args()

    summary = rebuild(args.db_path, args.ledger_dir, args.state_path)
    print(
        f"Rebuilt {summary['db_path']}: {summary['agents']} agent(s), "
        f"{summary['trades']} trade(s), {summary['accounts']} account snapshot(s), "
        f"{summary['open_positions_in_state']} open position(s) restored."
    )


if __name__ == "__main__":
    main()
