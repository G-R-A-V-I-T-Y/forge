import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    schema = SCHEMA_PATH.read_text()
    # Split on the "-- INDEXES" marker: tables must exist (and any pending
    # column migration must run) before indexes on new columns are created,
    # otherwise CREATE INDEX on e.g. trades(regime) fails against a
    # pre-existing local DB that predates that column.
    tables_sql, _, indexes_sql = schema.partition("-- INDEXES")
    conn.executescript(tables_sql)
    conn.commit()
    _migrate_trades_columns(conn)
    _migrate_positions_columns(conn)
    _migrate_agents_columns(conn)
    _migrate_equity_snapshots(conn)
    if indexes_sql:
        conn.executescript(indexes_sql)
        conn.commit()


# Columns added after the initial M1-M3 schema. CREATE TABLE IF NOT EXISTS
# above is a no-op against a pre-existing data/forge.db (now committed to
# git), so any column added here must also be backfilled via
# ALTER TABLE for users who already initialized a DB before this change.
_TRADES_MIGRATION_COLUMNS = {
    "ohlcv_15m_40_blob": "BLOB",
    "ohlcv_1h_20_blob": "BLOB",
    "ohlcv_4h_10_blob": "BLOB",
    "funding_history_blob": "BLOB",
    "oi_data_json": "TEXT",
    "liquidation_data_json": "TEXT",
    "regime": "TEXT",
    "expected_value_text": "TEXT",
    "funding_rate_current": "REAL",
    "open_interest_24h_change_pct": "REAL",
    "model_used": "TEXT",
    "fees_paid": "REAL",
    "funding_paid": "REAL",
    "duration_minutes": "REAL",
    "voided": "INTEGER NOT NULL DEFAULT 0",
    "void_reason": "TEXT",
    "true_notional": "REAL",
}

# Columns added after the initial schema for the positions table.
_POSITIONS_MIGRATION_COLUMNS = {
    "true_notional": "REAL",
    "max_hold_hours": "REAL NOT NULL DEFAULT 48.0",
}

# Columns added by the model-fallback-chain feature. Same rationale as
# _TRADES_MIGRATION_COLUMNS above: CREATE TABLE IF NOT EXISTS is a no-op
# against a pre-existing agents table, so this must be backfilled too.
_AGENTS_MIGRATION_COLUMNS = {
    "last_model_used": "TEXT",
    "wallet_address": "TEXT",
    "keystore_path": "TEXT",
    "live_enabled": "INTEGER DEFAULT 0",
    "active_spec_version": "INTEGER NOT NULL DEFAULT 0",
    "spawn_source": "TEXT DEFAULT 'fresh'",
}


def _migrate_trades_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add M4 fingerprint columns to an existing trades table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for col, sql_type in _TRADES_MIGRATION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {sql_type}")
    conn.commit()


def _migrate_positions_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add columns to an existing positions table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
    for col, sql_type in _POSITIONS_MIGRATION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {sql_type}")
    conn.commit()


def _migrate_agents_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add model-fallback-chain columns to an existing agents table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    for col, sql_type in _AGENTS_MIGRATION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {sql_type}")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def insert_agent(
    conn: sqlite3.Connection,
    agent_id: str,
    name: str,
    spawn_date: str,
    config_json: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, spawn_date, config_json) VALUES (?, ?, ?, ?)",
        (agent_id, name, spawn_date, config_json),
    )
    conn.commit()


def get_agent(conn: sqlite3.Connection, agent_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return dict(row) if row else None


def update_last_model_used(
    conn: sqlite3.Connection, agent_id: str, model_name: str | None
) -> None:
    """Record which model produced (or failed to produce) an agent's most
    recent decision cycle — "most recently used model", not "model used for
    the last trade": called after every wait/close/enter/error cycle, per
    agents/decision_loop.py's run_decision()."""
    conn.execute(
        "UPDATE agents SET last_model_used = ? WHERE id = ?",
        (model_name, agent_id),
    )
    conn.commit()


def insert_trade(conn: sqlite3.Connection, trade: dict) -> None:
    cols = list(trade.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT OR IGNORE INTO trades ({col_names}) VALUES ({placeholders})",
        list(trade.values()),
    )
    conn.commit()


def get_trades(conn: sqlite3.Connection, agent_id: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE agent_id = ? ORDER BY entry_timestamp DESC LIMIT ?",
        (agent_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_position(conn: sqlite3.Connection, position: dict) -> None:
    cols = list(position.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO positions ({col_names}) VALUES ({placeholders})",
        list(position.values()),
    )
    conn.commit()


def get_positions(conn: sqlite3.Connection, agent_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_position(conn: sqlite3.Connection, position_id: str) -> None:
    conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
    conn.commit()


def insert_account_snapshot(
    conn: sqlite3.Connection, agent_id: str, mode: str, balance: float, peak: float
) -> None:
    conn.execute(
        "INSERT INTO accounts (agent_id, mode, balance, peak_balance, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, mode, balance, peak, _now()),
    )
    conn.commit()


def get_latest_account(
    conn: sqlite3.Connection, agent_id: str, mode: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM accounts WHERE agent_id = ? AND mode = ? ORDER BY id DESC LIMIT 1",
        (agent_id, mode),
    ).fetchone()
    return dict(row) if row else None


def _migrate_equity_snapshots(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(equity_snapshots)")}
    if "id" not in existing:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS equity_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "recorded_at TEXT NOT NULL,"
            "total_equity REAL NOT NULL)"
        )
    existing_agent = {row["name"] for row in conn.execute("PRAGMA table_info(agent_equity_snapshots)")}
    if "id" not in existing_agent:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_equity_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "agent_id TEXT NOT NULL,"
            "balance REAL NOT NULL,"
            "recorded_at TEXT NOT NULL)"
        )
    conn.commit()


def capture_equity_snapshot(conn: sqlite3.Connection) -> None:
    """Snapshot every agent's paper balance and the portfolio total."""
    now = _now()
    rows = conn.execute(
        "SELECT agent_id, balance FROM accounts WHERE mode = 'paper' "
        "AND id IN (SELECT MAX(id) FROM accounts WHERE mode = 'paper' GROUP BY agent_id)"
    ).fetchall()
    if not rows:
        return
    total = sum(r["balance"] for r in rows)
    conn.execute(
        "INSERT INTO equity_snapshots (recorded_at, total_equity) VALUES (?, ?)",
        (now, total),
    )
    for r in rows:
        conn.execute(
            "INSERT INTO agent_equity_snapshots (agent_id, balance, recorded_at) VALUES (?, ?, ?)",
            (r["agent_id"], r["balance"], now),
        )
    conn.commit()


def get_portfolio_equity_history(
    conn: sqlite3.Connection, since: str, npoints: int = 200
) -> list[dict]:
    """Return evenly-spaced portfolio equity snapshots since `since` (ISO timestamp)."""
    rows = conn.execute(
        "SELECT recorded_at, total_equity FROM equity_snapshots "
        "WHERE recorded_at >= ? ORDER BY recorded_at ASC",
        (since,),
    ).fetchall()
    return [{"t": r["recorded_at"], "v": r["total_equity"]} for r in rows]


def get_agent_equity_history(
    conn: sqlite3.Connection, agent_id: str, since: str, npoints: int = 200
) -> list[dict]:
    """Return agent equity snapshots since `since` (ISO timestamp)."""
    rows = conn.execute(
        "SELECT recorded_at, balance FROM agent_equity_snapshots "
        "WHERE agent_id = ? AND recorded_at >= ? ORDER BY recorded_at ASC",
        (agent_id, since),
    ).fetchall()
    return [{"t": r["recorded_at"], "v": r["balance"]} for r in rows]


def void_corrupted_trades(conn: sqlite3.Connection) -> int:
    """Void trades that are structurally corrupted (missing SL/TP, wrong geometry, etc.)

    Returns the number of trades voided.
    """
    # Find trades with missing SL or TP (both should be non-null)
    cursor = conn.execute(
        """UPDATE trades
           SET voided = 1, void_reason = 'missing_stop_loss_or_take_profit'
           WHERE (stop_loss_price IS NULL OR take_profit_price IS NULL)
           AND voided = 0"""
    )

    # Find trades where SL/TP geometry is wrong for the direction
    # Long: SL should be < entry < TP
    # Short: TP should be < entry < SL
    cursor = conn.execute(
        """UPDATE trades 
           SET voided = 1, void_reason = 'invalid_sl_tp_geometry'
           WHERE voided = 0
           AND (
               (direction = 'long' AND (stop_loss_price >= entry_price OR entry_price >= take_profit_price))
               OR
               (direction = 'short' AND (take_profit_price >= entry_price OR entry_price >= stop_loss_price))
           )"""
    )

    # Find trades where SL distance is < 0.3% (too tight)
    cursor = conn.execute(
        """UPDATE trades 
           SET voided = 1, void_reason = 'stop_loss_too_tight'
           WHERE voided = 0
           AND ABS(entry_price - stop_loss_price) / entry_price < 0.003"""
    )

    # Find trades where TP distance is < 0.5% (below fee hurdle)
    cursor = conn.execute(
        """UPDATE trades 
           SET voided = 1, void_reason = 'take_profit_below_fee_hurdle'
           WHERE voided = 0
           AND ABS(take_profit_price - entry_price) / entry_price < 0.005"""
    )

    conn.commit()

    # Return count of voided trades
    cursor = conn.execute("SELECT COUNT(*) FROM trades WHERE voided = 1")
    return cursor.fetchone()[0]
