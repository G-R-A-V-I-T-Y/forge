CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'rookie',
    spawn_date TEXT NOT NULL,
    cull_date TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    current_thesis_version INTEGER NOT NULL DEFAULT 1,
    last_model_used TEXT,
    wallet_address TEXT,
    keystore_path TEXT,
    live_enabled INTEGER DEFAULT 0,
    active_spec_version INTEGER NOT NULL DEFAULT 0
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
    true_notional REAL,
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
    model_used TEXT,
    voided INTEGER NOT NULL DEFAULT 0,
    void_reason TEXT,
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
    true_notional REAL,
    opened_at TEXT NOT NULL,
    current_pnl_pct REAL DEFAULT 0.0,
    max_hold_hours REAL NOT NULL DEFAULT 48.0,
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
    review_required INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_reason TEXT,
    decision_details_json TEXT,
    counterfactual_result TEXT,
    counterfactual_was_better INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS seeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    pnl_pct REAL NOT NULL,
    thesis_excerpt TEXT,
    key_conditions_met TEXT,
    spawned_agent_id TEXT,
    used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    reflection_id INTEGER,
    claim TEXT NOT NULL,
    feature TEXT,
    direction TEXT,
    regime_context TEXT,
    predicted_effect TEXT NOT NULL,
    falsification_condition TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    effect_observed REAL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id),
    FOREIGN KEY (reflection_id) REFERENCES reflections(id)
);

CREATE TABLE IF NOT EXISTS entry_disables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    disabled_by TEXT NOT NULL DEFAULT 'human',
    disabled_at TEXT NOT NULL,
    reason TEXT,
    enabled_at TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    thesis_version INTEGER NOT NULL,
    yaml_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'inactive',
    deployed_at TEXT,
    rejection_reason TEXT,
    validation_errors TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_specs_agent ON specs(agent_id);
CREATE INDEX IF NOT EXISTS idx_specs_agent_status ON specs(agent_id, status);

-- INDEXES
-- (store/db.py.init_schema executes table-creation statements, then runs
-- the trades-column migration, then this block — so indexes on columns
-- added after the original M1-M3 schema, e.g. regime, can never run
-- against a table that doesn't have them yet.)
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT,
    action TEXT NOT NULL,
    details_json TEXT,
    performed_by TEXT NOT NULL DEFAULT 'human',
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_trades_agent ON trades(agent_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);
CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);
CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades(direction);
CREATE INDEX IF NOT EXISTS idx_positions_agent ON positions(agent_id);
CREATE INDEX IF NOT EXISTS idx_positions_asset ON positions(asset);
CREATE INDEX IF NOT EXISTS idx_accounts_agent ON accounts(agent_id);

CREATE TABLE IF NOT EXISTS decision_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    horizon TEXT NOT NULL,  -- '1h', '4h', '24h'
    fwd_return_pct REAL,
    max_runup_pct REAL,
    max_drawdown_pct REAL,
    chosen_outcome_pct REAL,
    best_action TEXT,  -- 'wait', 'enter_long', 'enter_short'
    best_outcome_pct REAL,
    regret_pct REAL,
    labeled_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_decision_labels_decision ON decision_labels(decision_id);
CREATE INDEX IF NOT EXISTS idx_decision_labels_horizon ON decision_labels(horizon);
CREATE INDEX IF NOT EXISTS idx_hypotheses_agent ON hypotheses(agent_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses(status);
