CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'rookie',
    spawn_date TEXT NOT NULL,
    cull_date TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    current_thesis_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS theses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    change_summary TEXT,
    adversarial_critique TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    thesis_version INTEGER NOT NULL DEFAULT 1,
    account_balance_at_entry REAL,
    mode TEXT NOT NULL DEFAULT 'paper',
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL,
    stop_loss_price REAL,
    take_profit_price REAL,
    leverage INTEGER,
    position_size_pct REAL,
    notional_usd REAL,
    entry_timestamp TEXT,
    exit_price REAL,
    exit_timestamp TEXT,
    exit_reason TEXT,
    duration_minutes REAL,
    pnl_pct REAL,
    pnl_usd REAL,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    market_context_json TEXT,
    agent_reasoning_json TEXT,
    postmortem TEXT,
    hypothesis TEXT,
    key_conditions_met TEXT,
    key_conditions_missing TEXT,
    confidence REAL,
    expected_value TEXT,
    agent_postmortem TEXT,
    ohlcv_15m_40_blob BLOB,
    ohlcv_1h_20_blob BLOB,
    ohlcv_4h_10_blob BLOB,
    funding_history_blob BLOB,
    oi_data_json TEXT,
    liquidation_data_json TEXT,
    regime TEXT,
    expected_value_text TEXT,
    funding_rate_current REAL,
    open_interest_24h_change_pct REAL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    balance REAL NOT NULL,
    peak_balance REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    leverage INTEGER NOT NULL,
    position_size_pct REAL NOT NULL,
    notional_usd REAL NOT NULL,
    opened_at TEXT NOT NULL,
    current_pnl_pct REAL DEFAULT 0.0,
    mode TEXT NOT NULL DEFAULT 'paper',
    trade_id TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id),
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    trades_since_last INTEGER,
    evidence_summary TEXT,
    research_queries_json TEXT,
    research_findings_json TEXT,
    proposed_changes TEXT,
    adversarial_critique TEXT,
    holdout_result TEXT,
    outcome TEXT,
    rejection_reason TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    trades_evaluated INTEGER,
    metrics_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string',
    label TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- INDEXES
-- (store/db.py.init_schema executes table-creation statements, then runs
-- the trades-column migration, then this block — so indexes on columns
-- added after the original M1-M3 schema, e.g. regime, can never run
-- against a table that doesn't have them yet.)
CREATE INDEX IF NOT EXISTS idx_trades_agent ON trades(agent_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);
CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);
CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades(direction);
CREATE INDEX IF NOT EXISTS idx_positions_agent ON positions(agent_id);
CREATE INDEX IF NOT EXISTS idx_positions_asset ON positions(asset);
CREATE INDEX IF NOT EXISTS idx_accounts_agent ON accounts(agent_id);
