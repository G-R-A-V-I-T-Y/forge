import sqlite3
from datetime import datetime, timezone
import pytest
from store.db import (
    get_connection, init_schema, insert_agent, get_agent,
    insert_trade, get_trades, insert_position, get_positions,
    delete_position, insert_account_snapshot, get_latest_account,
    update_last_model_used, _TRADES_MIGRATION_COLUMNS, _AGENTS_MIGRATION_COLUMNS,
)


def test_init_schema_creates_all_tables(tmp_path):
    db_file = str(tmp_path / "test.db")
    conn = get_connection(db_file)
    init_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"agents", "theses", "trades", "accounts", "positions",
                "reflections", "evaluations", "settings", "chat_history"}
    assert expected.issubset(tables)
    conn.close()


def test_insert_and_get_agent(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    agent = get_agent(conn, "jade_hawk")
    assert agent is not None
    assert agent["name"] == "jade_hawk"
    assert agent["status"] == "rookie"


def test_get_agent_missing_returns_none(conn):
    assert get_agent(conn, "does_not_exist") is None


def test_insert_and_get_trades(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    trade = {
        "id": "jade_hawk_20260629_143712_SOL",
        "agent_id": "jade_hawk",
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 145.20,
        "stop_loss_price": 143.00,
        "take_profit_price": 152.00,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-06-29T14:37:12Z",
        "status": "open",
    }
    insert_trade(conn, trade)
    trades = get_trades(conn, "jade_hawk", limit=10)
    assert len(trades) == 1
    assert trades[0]["asset"] == "SOL-PERP"


def test_positions_crud(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    trade = {
        "id": "jade_hawk_20260629_143712_SOL",
        "agent_id": "jade_hawk",
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 145.20,
        "stop_loss_price": 143.00,
        "take_profit_price": 152.00,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-06-29T14:37:12Z",
        "status": "open",
    }
    insert_trade(conn, trade)
    pos = {
        "id": "pos_jade_hawk_20260629_143712_SOL",
        "agent_id": "jade_hawk",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 145.20,
        "stop_loss_price": 143.00,
        "take_profit_price": 152.00,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "opened_at": "2026-06-29T14:37:12Z",
        "mode": "paper",
        "trade_id": "jade_hawk_20260629_143712_SOL",
    }
    insert_position(conn, pos)
    positions = get_positions(conn, "jade_hawk")
    assert len(positions) == 1
    delete_position(conn, "pos_jade_hawk_20260629_143712_SOL")
    assert get_positions(conn, "jade_hawk") == []


def test_account_snapshot(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "jade_hawk", "paper", 50000.0, 50000.0)
    insert_account_snapshot(conn, "jade_hawk", "paper", 51000.0, 51000.0)
    latest = get_latest_account(conn, "jade_hawk", "paper")
    assert latest["balance"] == 51000.0


def test_init_schema_adds_m4_fingerprint_columns(tmp_path):
    db_file = str(tmp_path / "test.db")
    conn = get_connection(db_file)
    init_schema(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for col in _TRADES_MIGRATION_COLUMNS:
        assert col in columns
    conn.close()


def test_update_last_model_used(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    assert get_agent(conn, "jade_hawk")["last_model_used"] is None
    update_last_model_used(conn, "jade_hawk", "Big Pickle")
    assert get_agent(conn, "jade_hawk")["last_model_used"] == "Big Pickle"
    update_last_model_used(conn, "jade_hawk", "no model available")
    assert get_agent(conn, "jade_hawk")["last_model_used"] == "no model available"


def test_init_schema_adds_model_fallback_chain_columns(tmp_path):
    """agents.last_model_used and trades.model_used — the model-fallback-
    chain feature's schema additions — exist on a fresh database."""
    db_file = str(tmp_path / "test.db")
    conn = get_connection(db_file)
    init_schema(conn)
    agent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    for col in _AGENTS_MIGRATION_COLUMNS:
        assert col in agent_columns
    trade_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    assert "model_used" in trade_columns
    conn.close()


def test_init_schema_migration_is_idempotent_on_pre_model_chain_db(tmp_path):
    """Simulates a local data/forge.db created before this feature: agents
    table exists without last_model_used, trades table exists without
    model_used. init_schema() must backfill both via ALTER TABLE without
    erroring on a second call — same pattern as the pre-M4 migration test
    above."""
    db_file = str(tmp_path / "legacy.db")
    conn = get_connection(db_file)
    conn.executescript("""
        CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'rookie', spawn_date TEXT NOT NULL,
            cull_date TEXT, config_json TEXT NOT NULL DEFAULT '{}',
            current_thesis_version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            thesis_version INTEGER NOT NULL DEFAULT 1,
            account_balance_at_entry REAL, mode TEXT NOT NULL DEFAULT 'paper',
            asset TEXT NOT NULL, direction TEXT NOT NULL, entry_price REAL,
            stop_loss_price REAL, take_profit_price REAL, leverage INTEGER,
            position_size_pct REAL, notional_usd REAL, entry_timestamp TEXT,
            exit_price REAL, exit_timestamp TEXT, exit_reason TEXT,
            duration_minutes REAL, pnl_pct REAL, pnl_usd REAL, result TEXT,
            status TEXT NOT NULL DEFAULT 'open', market_context_json TEXT,
            agent_reasoning_json TEXT, postmortem TEXT, hypothesis TEXT,
            key_conditions_met TEXT, key_conditions_missing TEXT,
            confidence REAL, expected_value TEXT, agent_postmortem TEXT
        );
    """)
    conn.commit()
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    conn.execute("INSERT INTO trades (id, agent_id, asset, direction) VALUES (?, ?, ?, ?)",
                 ("t1", "jade_hawk", "SOL-PERP", "long"))
    conn.commit()

    init_schema(conn)  # should ALTER TABLE both tables, not fail
    agent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    for col in _AGENTS_MIGRATION_COLUMNS:
        assert col in agent_columns
    trade_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    assert "model_used" in trade_columns

    # pre-existing rows survive the migration
    agent = get_agent(conn, "jade_hawk")
    assert agent["last_model_used"] is None
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["model_used"] is None

    init_schema(conn)  # second call must be a no-op, not raise "duplicate column"
    conn.close()


def test_init_schema_adds_decision_labels_and_hypotheses_tables(tmp_path):
    """Simulates a local data/forge.db created before M10 labeling: agents/
    decisions/trades exist but decision_labels and hypotheses do not (both
    are whole new tables, not new columns on an existing one, so
    CREATE TABLE IF NOT EXISTS in schema.sql is sufficient -- no
    ALTER TABLE migration function needed). init_schema() must create both
    without erroring and without touching pre-existing rows, and a second
    call must be a no-op."""
    db_file = str(tmp_path / "pre_m10.db")
    conn = get_connection(db_file)
    conn.executescript("""
        CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'rookie', spawn_date TEXT NOT NULL,
            cull_date TEXT, config_json TEXT NOT NULL DEFAULT '{}',
            current_thesis_version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, decision_action TEXT NOT NULL,
            decision_reason TEXT, decision_details_json TEXT,
            counterfactual_result TEXT,
            counterfactual_was_better INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            thesis_version INTEGER NOT NULL DEFAULT 1,
            account_balance_at_entry REAL, mode TEXT NOT NULL DEFAULT 'paper',
            asset TEXT NOT NULL, direction TEXT NOT NULL, entry_price REAL,
            stop_loss_price REAL, take_profit_price REAL, leverage INTEGER,
            position_size_pct REAL, notional_usd REAL, entry_timestamp TEXT,
            exit_price REAL, exit_timestamp TEXT, exit_reason TEXT,
            duration_minutes REAL, pnl_pct REAL, pnl_usd REAL, result TEXT,
            status TEXT NOT NULL DEFAULT 'open', market_context_json TEXT,
            agent_reasoning_json TEXT, postmortem TEXT, hypothesis TEXT,
            key_conditions_met TEXT, key_conditions_missing TEXT,
            confidence REAL, expected_value TEXT, agent_postmortem TEXT
        );
    """)
    conn.commit()
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    conn.execute(
        "INSERT INTO decisions (agent_id, timestamp, decision_action) VALUES (?, ?, ?)",
        ("jade_hawk", "2026-06-29T00:00:00Z", "wait"),
    )
    conn.commit()

    tables_before = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "decision_labels" not in tables_before
    assert "hypotheses" not in tables_before

    init_schema(conn)  # should CREATE TABLE both, not fail

    tables_after = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "decision_labels" in tables_after
    assert "hypotheses" in tables_after

    # Pre-existing row survives the migration.
    row = dict(conn.execute(
        "SELECT * FROM decisions WHERE agent_id = ?", ("jade_hawk",)
    ).fetchone())
    assert row["decision_action"] == "wait"

    init_schema(conn)  # second call must be a no-op, not raise
    conn.close()


def test_init_schema_migration_is_idempotent_on_pre_m4_db(tmp_path):
    """Simulates a local data/forge.db created before M4: trades table exists
    without the new fingerprint columns. init_schema() must backfill them
    via ALTER TABLE without erroring on a second call."""
    db_file = str(tmp_path / "legacy.db")
    conn = get_connection(db_file)
    # Mirrors the pre-M4 (M1-M3) trades table: no ohlcv blobs, no regime,
    # no funding/OI/liquidation columns — everything else M4 builds on.
    conn.executescript("""
        CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'rookie', spawn_date TEXT NOT NULL,
            cull_date TEXT, config_json TEXT NOT NULL DEFAULT '{}',
            current_thesis_version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            thesis_version INTEGER NOT NULL DEFAULT 1,
            account_balance_at_entry REAL, mode TEXT NOT NULL DEFAULT 'paper',
            asset TEXT NOT NULL, direction TEXT NOT NULL, entry_price REAL,
            stop_loss_price REAL, take_profit_price REAL, leverage INTEGER,
            position_size_pct REAL, notional_usd REAL, entry_timestamp TEXT,
            exit_price REAL, exit_timestamp TEXT, exit_reason TEXT,
            duration_minutes REAL, pnl_pct REAL, pnl_usd REAL, result TEXT,
            status TEXT NOT NULL DEFAULT 'open', market_context_json TEXT,
            agent_reasoning_json TEXT, postmortem TEXT, hypothesis TEXT,
            key_conditions_met TEXT, key_conditions_missing TEXT,
            confidence REAL, expected_value TEXT, agent_postmortem TEXT
        );
    """)
    conn.commit()
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    conn.execute("INSERT INTO trades (id, agent_id, asset, direction) VALUES (?, ?, ?, ?)",
                 ("t1", "jade_hawk", "SOL-PERP", "long"))
    conn.commit()

    init_schema(conn)  # should ALTER TABLE the existing trades table, not fail
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for col in _TRADES_MIGRATION_COLUMNS:
        assert col in columns

    # pre-existing row survives the migration
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["asset"] == "SOL-PERP"
    assert row["regime"] is None

    init_schema(conn)  # second call must be a no-op, not raise "duplicate column"
    conn.close()
