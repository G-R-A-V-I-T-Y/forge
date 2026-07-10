#!/usr/bin/env python
"""scripts/rebuild_local_cache.py -- Disaster-recovery rebuild of data/forge.db.

Reconstructs the local, gitignored SQLite cache purely from the git-tracked
ledger/ and state/ directories. This is the concrete proof of the "burned
laptop -> git pull -> back to normal" requirement: after cloning the repo
fresh, run this once before `python forge.py`. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.

Scope: restores everything the trading loop needs to keep operating without
skipping a beat -- agents, balances, closed trades, and open positions
(capital actually at risk). It deliberately does NOT replay the `decisions`
or market-data (candles/funding/oi/liquidations) ledger streams into
SQLite: those are high-volume analytical/calibration archives, not
operational state the trading loop reads on its hot path (the nightly
counterfactual job and any decision-history UI are the only consumers, and
neither is on the "must resume immediately" critical path). That history
isn't lost -- it's queryable directly from `ledger/decisions/*` and
`ledger/candles_5m/*` etc. (JSONL for the current month, Parquet for closed
months) exactly as it always was; duplicating it into the disposable cache
would also require either a schema migration (SQLite's `decisions` table
has no columns for confidence/evidence_strength/model, which only exist in
the ledger record) or a lossy remap, for a stream this codebase treats as
append-only history rather than mutable operational state.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
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

logger = logging.getLogger(__name__)


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
        insert_agent(conn, agent["id"], agent["name"], state["generated_at"], agent.get("config_json", "{}"))
        conn.execute(
            "UPDATE agents SET status = ?, current_thesis_version = ?, last_model_used = ? WHERE id = ?",
            (agent["status"], agent["current_thesis_version"], agent["last_model_used"], agent["id"]),
        )
    conn.commit()

    # --- Restore active specs from agents/specs/{id}_v*.yaml files --------
    from backtest.dsl import load_spec  # noqa: PLC0415 – lazy import avoids cycle
    from store.specs import deploy_spec, SPECS_DIR  # noqa: PLC0415

    restored_specs = 0
    for agent in state["agents"]:
        agent_id = agent["id"]
        spec_files = sorted(SPECS_DIR.glob(f"{agent_id}_v*.yaml"))
        for spec_path in spec_files:
            # Skip if this spec version already exists in the DB
            existing = conn.execute(
                "SELECT 1 FROM specs WHERE agent_id = ? AND spec_version = ?",
                (agent_id, spec_path.stem.split("_v")[-1]),
            ).fetchone()
            if existing is not None:
                continue
            try:
                spec = load_spec(str(spec_path))
                deploy_spec(conn, agent_id, spec, config=None)
                restored_specs += 1
            except Exception:
                logger.warning(
                    "failed to restore spec %s for %s", spec_path.name, agent_id,
                    exc_info=True,
                )

    trades_df = _read_partitions(ledger_dir, "trades")
    for _, row in trades_df.iterrows():
        try:
            insert_trade(conn, row.dropna().to_dict())
        except Exception as exc:
            raise RuntimeError(
                f"Failed to replay trade {row.get('id')!r} for agent "
                f"{row.get('agent_id')!r} -- is this agent missing from "
                f"state/current.json's agents list? ({exc})"
            ) from exc

    accounts_df = _read_partitions(ledger_dir, "accounts")
    for _, row in accounts_df.iterrows():
        try:
            insert_account_snapshot(conn, row["agent_id"], row["mode"], row["balance"], row["peak_balance"])
        except Exception as exc:
            raise RuntimeError(
                f"Failed to replay account snapshot for agent "
                f"{row.get('agent_id')!r} mode {row.get('mode')!r} -- is this "
                f"agent missing from state/current.json's agents list? ({exc})"
            ) from exc

    for agent in state["agents"]:
        paper_balance = agent.get("paper_balance")
        paper_peak = agent.get("paper_peak")
        if paper_balance is not None:
            insert_account_snapshot(
                conn, agent["id"], "paper", paper_balance,
                paper_peak if paper_peak is not None else paper_balance,
            )
        else:
            insert_account_snapshot(conn, agent["id"], "paper", 50000.0, 50000.0)

    for position in state.get("open_positions", []):
        try:
            trade_id = position.get("trade_id")
            if trade_id is not None:
                # No closed-trade ledger record exists for a still-open position
                # (execute_close only ever ledger-exports on CLOSE) -- synthesize
                # a minimal "open" trades row from the position snapshot so
                # insert_position's FK (positions.trade_id -> trades.id) is
                # satisfiable. insert_trade's INSERT OR IGNORE makes this a no-op
                # if a real (closed) record for this id was already replayed
                # from the ledger above.
                insert_trade(conn, {
                    "id": trade_id,
                    "agent_id": position["agent_id"],
                    "mode": position.get("mode", "paper"),
                    "asset": position["asset"],
                    "direction": position["direction"],
                    "entry_price": position.get("entry_price"),
                    "stop_loss_price": position.get("stop_loss_price"),
                    "take_profit_price": position.get("take_profit_price"),
                    "leverage": position.get("leverage"),
                    "position_size_pct": position.get("position_size_pct"),
                    "notional_usd": position.get("notional_usd"),
                    "entry_timestamp": position.get("opened_at"),
                    "status": "open",
                })
            insert_position(conn, position)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to reopen position {position.get('id')!r} for agent "
                f"{position.get('agent_id')!r} -- is this agent missing from "
                f"state/current.json's agents list? ({exc})"
            ) from exc

    conn.close()

    return {
        "db_path": str(db_path),
        "agents": len(state["agents"]),
        "specs_restored": restored_specs,
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

    try:
        summary = rebuild(args.db_path, args.ledger_dir, args.state_path)
    except Exception as exc:
        print(f"rebuild failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"Rebuilt {summary['db_path']}: {summary['agents']} agent(s), "
        f"{summary['specs_restored']} spec(s) restored, "
        f"{summary['trades']} trade(s), {summary['accounts']} account snapshot(s), "
        f"{summary['open_positions_in_state']} open position(s) restored.\n"
        f"Note: decision history and market data are not replayed into "
        f"{summary['db_path']} -- query them directly from ledger/decisions/ "
        f"and ledger/candles_5m/ etc."
    )


if __name__ == "__main__":
    main()
