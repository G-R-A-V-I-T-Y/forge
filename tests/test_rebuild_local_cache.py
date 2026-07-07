import json
import sqlite3
from pathlib import Path

import pytest

from scripts.rebuild_local_cache import rebuild


def _write_state(path: Path, agents: list[dict], open_positions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": "2026-07-06T12:00:00Z", "agents": agents, "open_positions": open_positions}),
        encoding="utf-8",
    )


def _write_ledger_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_rebuild_refuses_to_overwrite_existing_db(tmp_path):
    db_path = tmp_path / "forge.db"
    db_path.write_text("not empty")
    state_path = tmp_path / "state" / "current.json"
    _write_state(state_path, [], [])

    with pytest.raises(FileExistsError):
        rebuild(db_path, tmp_path / "ledger", state_path)


def test_rebuild_seeds_agents_and_balances_from_state(tmp_path):
    db_path = tmp_path / "forge.db"
    state_path = tmp_path / "state" / "current.json"
    _write_state(
        state_path,
        agents=[{
            "id": "sage_turtle", "name": "sage_turtle", "status": "active",
            "current_thesis_version": 2, "last_model_used": "qwen3.6-35b",
            "paper_balance": 51200.0, "paper_peak": 52000.0, "live_balance": None,
        }],
        open_positions=[],
    )

    summary = rebuild(db_path, tmp_path / "ledger", state_path)

    assert summary["agents"] == 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agents WHERE id = 'sage_turtle'").fetchone()
    assert row["status"] == "active"
    assert row["current_thesis_version"] == 2
    conn.close()


def test_rebuild_replays_trades_and_accounts_from_ledger(tmp_path):
    db_path = tmp_path / "forge.db"
    state_path = tmp_path / "state" / "current.json"
    _write_state(
        state_path,
        agents=[{
            "id": "sage_turtle", "name": "sage_turtle", "status": "active",
            "current_thesis_version": 1, "last_model_used": None,
            "paper_balance": 50500.0, "paper_peak": 50500.0, "live_balance": None,
        }],
        open_positions=[],
    )
    _write_ledger_jsonl(
        tmp_path / "ledger" / "trades" / "2026-07.jsonl",
        [{
            "id": "t1", "agent_id": "sage_turtle", "mode": "paper", "asset": "FET-PERP",
            "direction": "short", "entry_price": 1.5, "exit_price": 1.44,
            "status": "closed", "result": "win",
        }],
    )
    _write_ledger_jsonl(
        tmp_path / "ledger" / "accounts" / "2026-07.jsonl",
        [{"agent_id": "sage_turtle", "mode": "paper", "balance": 50500.0, "peak_balance": 50500.0}],
    )

    summary = rebuild(db_path, tmp_path / "ledger", state_path)

    assert summary["trades"] == 1
    assert summary["accounts"] == 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM trades WHERE id = 't1'").fetchone()
    assert trade["status"] == "closed"
    assert trade["exit_price"] == 1.44
    conn.close()


def test_rebuild_reopens_positions_from_state(tmp_path):
    db_path = tmp_path / "forge.db"
    state_path = tmp_path / "state" / "current.json"
    open_position = {
        "id": "pos_t2", "agent_id": "sage_turtle", "asset": "TIA-PERP", "direction": "long",
        "entry_price": 4.2, "stop_loss_price": 4.0, "take_profit_price": 4.6,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "opened_at": "2026-07-06T11:00:00Z", "mode": "paper", "trade_id": "t2",
    }
    _write_state(
        state_path,
        agents=[{
            "id": "sage_turtle", "name": "sage_turtle", "status": "active",
            "current_thesis_version": 1, "last_model_used": None,
            "paper_balance": 50000.0, "paper_peak": 50000.0, "live_balance": None,
        }],
        open_positions=[open_position],
    )
    # No ledger/trades record for t2 -- execute_close only ever ledger-exports
    # on CLOSE, so a still-open position's trade row exists only in the (lost)
    # local SQLite DB, never in the git-tracked ledger. This is the real
    # scenario rebuild() must handle.

    summary = rebuild(db_path, tmp_path / "ledger", state_path)

    assert summary["open_positions_in_state"] == 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    pos = conn.execute("SELECT * FROM positions WHERE id = 'pos_t2'").fetchone()
    assert pos is not None
    assert pos["asset"] == "TIA-PERP"
    conn.close()
