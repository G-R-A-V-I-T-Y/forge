# Forge Milestone 1 — Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One agent (`jade_hawk`) wakes on a 60-second schedule, makes a stub trade decision, records a full fingerprint to SQLite, maintains a paper account, and the web UI at `localhost:8000` shows it happening in real time.

**Architecture:** Single Python process using APScheduler for agent wakeups and uvicorn for the web server, both running concurrently in the same asyncio event loop. SQLite is the only persistent store. Stub implementations replace real LLM and real market data so every seam is testable without external dependencies.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, uvicorn, APScheduler, PyYAML, pytest, pytest-asyncio

## Global Constraints

- Python 3.11+ (use `asyncio.TaskGroup` not `asyncio.gather` patterns)
- SQLite WAL mode always — concurrent reads from web + writes from agents
- All datetimes stored as ISO-8601 UTC strings (`datetime.utcnow().isoformat() + "Z"`)
- Trade IDs format: `{agent_id}_{YYYYMMDD}_{HHMMSS}_{asset_short}` e.g. `jade_hawk_20260629_143712_SOL`
- No secrets in config.yaml — only in `.env`
- Every module under `agents/`, `store/`, `market/`, `risk/`, `execution/`, `llm/`, `web/` must have `__init__.py`
- All exceptions caught in the runtime loop — nothing kills the scheduler
- Working directory for all commands: `C:\Users\chris\OneDrive\Documents\AgentWorkspace\forge`

---

## File Map

| File | Responsibility |
|------|---------------|
| `forge.py` | Entry point: init DB, register agent, start scheduler + web server |
| `config.yaml` | Desk-wide defaults (no secrets) |
| `.env.example` | Documents required env vars |
| `requirements.txt` | Pinned dependencies |
| `data/schema.sql` | All SQLite table definitions |
| `store/db.py` | SQLite connection pool, schema init, CRUD helpers |
| `market/stub.py` | Hardcoded deterministic market data (OHLCV, funding, OI, liquidations) |
| `risk/gate.py` | Stateless order validator; raises `RiskViolation` on failure |
| `execution/bridge.py` | `TradingBridge` ABC |
| `execution/paper_bridge.py` | Simulates fills at stub prices, writes trades/positions/accounts |
| `agents/persona.py` | Builds the LLM system prompt from agent config |
| `llm/stub.py` | Returns hardcoded valid trade decision JSON — no LLM call |
| `agents/prompt_builder.py` | Assembles full decision prompt (thesis + performance + market state) |
| `agents/decision_loop.py` | One full decision cycle: data → prompt → LLM → validate → risk → execute |
| `agents/runtime.py` | APScheduler job wrapper; catches exceptions, logs, never crashes |
| `web/app.py` | FastAPI app with GET `/` overview page |
| `web/templates/base.html` | Base HTML template with minimal styling |
| `web/templates/overview.html` | Overview: agent balance + last 10 trades table |
| `agents/theses/jade_hawk_v1.md` | Initial thesis for jade_hawk |
| `tests/test_risk_gate.py` | Tests for risk gate validation logic |
| `tests/test_paper_bridge.py` | Tests for paper bridge fill + DB writes |
| `tests/test_decision_loop.py` | Integration test: full stub decision cycle |
| `tests/conftest.py` | Shared pytest fixtures (in-memory SQLite DB) |

---

### Task 1: Project Scaffold

**Files:**
- Create: `.gitignore`
- Create: `requirements.txt`
- Create: `config.yaml`
- Create: `.env.example`
- Create: `data/schema.sql`
- Create: `__init__.py` in every package directory

- [ ] **Step 1: Initialize git repo**

```bash
cd C:\Users\chris\OneDrive\Documents\AgentWorkspace\forge
git init
git checkout -b main
```

- [ ] **Step 2: Write `.gitignore`**

```
.env
*.pyc
__pycache__/
*.db
*.db-wal
*.db-shm
data/forge.db
data/backups/
logs/
.pytest_cache/
*.egg-info/
dist/
build/
.venv/
venv/
```

- [ ] **Step 3: Write `requirements.txt`**

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
jinja2==3.1.4
apscheduler==3.10.4
pyyaml==6.0.2
httpx==0.27.2
pytest==8.3.3
pytest-asyncio==0.24.0
```

- [ ] **Step 4: Write `config.yaml`**

```yaml
universe:
  - BTC-PERP
  - ETH-PERP
  - SOL-PERP
  - BNB-PERP
  - XRP-PERP
  - DOGE-PERP
  - AVAX-PERP
  - LINK-PERP
  - ARB-PERP
  - OP-PERP
  - SUI-PERP
  - TON-PERP
  - PEPE-PERP
  - WIF-PERP
  - TRUMP-PERP

desk:
  max_leverage: 10
  max_position_size_pct: 0.20
  max_concurrent_positions: 3
  wake_interval_seconds: 60
  starting_balance: 50000.0
  target_agent_count: 8
  drawdown_kill_pct: 0.15

data_source: stub
llm_backend: stub
```

- [ ] **Step 5: Write `.env.example`**

```
# Hyperliquid (required for Milestone 2+)
HL_WALLET_PRIVATE_KEY=your_private_key_here

# Search API (required for Milestone 6+)
SEARCH_API_KEY=your_brave_or_serp_key_here

# Webhook alerts (optional, Milestone 9+)
ALERT_WEBHOOK_URL=https://hooks.slack.com/...
```

- [ ] **Step 6: Write `data/schema.sql`**

```sql
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

CREATE INDEX IF NOT EXISTS idx_trades_agent ON trades(agent_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_positions_agent ON positions(agent_id);
CREATE INDEX IF NOT EXISTS idx_positions_asset ON positions(asset);
CREATE INDEX IF NOT EXISTS idx_accounts_agent ON accounts(agent_id);
```

- [ ] **Step 7: Create `__init__.py` in all packages**

```bash
# Run from forge/ directory
for dir in agents market risk execution store llm meta web; do
    touch "$dir/__init__.py"
done
touch tests/__init__.py
```

On Windows PowerShell:
```powershell
foreach ($dir in @("agents","market","risk","execution","store","llm","meta","web","tests")) {
    New-Item -ItemType File -Force "$dir\__init__.py" | Out-Null
}
```

- [ ] **Step 8: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: all packages install without error.

- [ ] **Step 9: Initial commit**

```bash
git add .gitignore requirements.txt config.yaml .env.example data/schema.sql agents/__init__.py market/__init__.py risk/__init__.py execution/__init__.py store/__init__.py llm/__init__.py meta/__init__.py web/__init__.py tests/__init__.py
git commit -m "feat: project scaffold — directory structure, schema, config"
```

---

### Task 2: Database Layer (`store/db.py`)

**Files:**
- Create: `store/db.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `get_connection(db_path: str) -> sqlite3.Connection` — WAL-mode connection
  - `init_schema(conn: sqlite3.Connection) -> None` — runs schema.sql
  - `insert_agent(conn, agent_id, name, spawn_date, config_json) -> None`
  - `get_agent(conn, agent_id) -> dict | None`
  - `insert_trade(conn, trade: dict) -> None`
  - `get_trades(conn, agent_id: str, limit: int = 10) -> list[dict]`
  - `insert_position(conn, position: dict) -> None`
  - `get_positions(conn, agent_id: str) -> list[dict]`
  - `delete_position(conn, position_id: str) -> None`
  - `insert_account_snapshot(conn, agent_id, mode, balance, peak) -> None`
  - `get_latest_account(conn, agent_id, mode) -> dict | None`

- [ ] **Step 1: Write `tests/conftest.py`**

```python
import sqlite3
import pytest
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    schema = SCHEMA_PATH.read_text()
    c.executescript(schema)
    yield c
    c.close()
```

- [ ] **Step 2: Write failing tests in `tests/test_db.py`**

```python
import sqlite3
from datetime import datetime, timezone
import pytest
from store.db import (
    get_connection, init_schema, insert_agent, get_agent,
    insert_trade, get_trades, insert_position, get_positions,
    delete_position, insert_account_snapshot, get_latest_account,
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
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
pytest tests/test_db.py -v
```

Expected: `ImportError: cannot import name 'get_connection' from 'store.db'`

- [ ] **Step 4: Implement `store/db.py`**

```python
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
    conn.executescript(schema)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def insert_agent(conn: sqlite3.Connection, agent_id: str, name: str,
                 spawn_date: str, config_json: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, spawn_date, config_json) VALUES (?, ?, ?, ?)",
        (agent_id, name, spawn_date, config_json),
    )
    conn.commit()


def get_agent(conn: sqlite3.Connection, agent_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return dict(row) if row else None


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


def insert_account_snapshot(conn: sqlite3.Connection, agent_id: str,
                             mode: str, balance: float, peak: float) -> None:
    conn.execute(
        "INSERT INTO accounts (agent_id, mode, balance, peak_balance, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, mode, balance, peak, _now()),
    )
    conn.commit()


def get_latest_account(conn: sqlite3.Connection, agent_id: str,
                        mode: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM accounts WHERE agent_id = ? AND mode = ? ORDER BY id DESC LIMIT 1",
        (agent_id, mode),
    ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
pytest tests/test_db.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add store/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: SQLite store — schema init, CRUD helpers for all tables"
```

---

### Task 3: Risk Gate (`risk/gate.py`)

**Files:**
- Create: `risk/gate.py`
- Create: `tests/test_risk_gate.py`

**Interfaces:**
- Consumes: nothing from prior tasks
- Produces:
  - `class RiskViolation(Exception)` — carries `.reason: str`
  - `validate_order(order: dict, account_balance: float, config: dict, open_position_count: int) -> None` — raises `RiskViolation` on any violation, returns `None` on pass

Order dict fields used by validate_order:
```python
{
  "asset": "SOL-PERP",
  "direction": "long",       # "long" | "short"
  "entry_price": 145.20,
  "stop_loss_price": 143.00,
  "take_profit_price": 152.00,
  "leverage": 3,
  "position_size_pct": 0.10,  # fraction of account balance
}
```

Config dict keys used: `max_leverage`, `max_position_size_pct`, `max_concurrent_positions`

- [ ] **Step 1: Write failing tests in `tests/test_risk_gate.py`**

```python
import pytest
from risk.gate import RiskViolation, validate_order

CONFIG = {
    "max_leverage": 10,
    "max_position_size_pct": 0.20,
    "max_concurrent_positions": 3,
}
BALANCE = 50000.0

VALID_ORDER = {
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
}


def test_valid_order_passes():
    validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=0)


def test_missing_stop_loss_raises():
    order = {**VALID_ORDER}
    del order["stop_loss_price"]
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "stop_loss" in exc.value.reason.lower()


def test_stop_loss_too_close_raises():
    # SL must be >= 0.3% from entry; 0.1% is too close
    entry = 145.20
    sl = entry * (1 - 0.001)  # 0.1% below entry
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "stop loss distance" in exc.value.reason.lower()


def test_leverage_over_cap_raises():
    order = {**VALID_ORDER, "leverage": 11}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "leverage" in exc.value.reason.lower()


def test_position_size_over_cap_raises():
    order = {**VALID_ORDER, "position_size_pct": 0.25}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "position size" in exc.value.reason.lower()


def test_too_many_open_positions_raises():
    with pytest.raises(RiskViolation) as exc:
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=3)
    assert "concurrent positions" in exc.value.reason.lower()


def test_liquidation_price_too_close_raises():
    # Liquidation for 10x long ≈ entry * (1 - 1/leverage)
    # entry=145.20, leverage=10 → liq ≈ 130.68 (distance = 14.52)
    # SL at 143.00 → SL distance = 2.20
    # Requirement: liq must be >= 2x SL distance from entry
    # 14.52 >= 2 * 2.20 = 4.40  → this passes.
    # To fail: leverage=10, SL very close to liq
    entry = 145.20
    sl = entry * (1 - 0.06)   # 6% below entry — SL distance = 8.71
    # liq at 10x = 145.20 * (1 - 0.1) = 130.68 → liq distance = 14.52
    # need liq_dist >= 2 * sl_dist → 14.52 >= 17.42 → FAILS
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl, "leverage": 10}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "liquidation" in exc.value.reason.lower()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_risk_gate.py -v
```

Expected: `ImportError` — `risk.gate` does not exist.

- [ ] **Step 3: Implement `risk/gate.py`**

```python
MIN_SL_DISTANCE_PCT = 0.003   # 0.3% minimum


class RiskViolation(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_order(order: dict, account_balance: float,
                   config: dict, open_position_count: int) -> None:
    """Raises RiskViolation on any breach. Returns None on pass."""

    if "stop_loss_price" not in order or order.get("stop_loss_price") is None:
        raise RiskViolation("stop_loss_price is required")

    entry = order["entry_price"]
    sl = order["stop_loss_price"]
    direction = order["direction"]
    leverage = order["leverage"]
    size_pct = order["position_size_pct"]

    # SL distance check
    sl_dist = abs(entry - sl) / entry
    if sl_dist < MIN_SL_DISTANCE_PCT:
        raise RiskViolation(
            f"stop loss distance {sl_dist:.4%} is below minimum {MIN_SL_DISTANCE_PCT:.4%}"
        )

    # Leverage cap
    if leverage > config["max_leverage"]:
        raise RiskViolation(
            f"leverage {leverage}x exceeds max {config['max_leverage']}x"
        )

    # Position size cap
    if size_pct > config["max_position_size_pct"]:
        raise RiskViolation(
            f"position size {size_pct:.0%} exceeds max {config['max_position_size_pct']:.0%}"
        )

    # Concurrent positions cap
    if open_position_count >= config["max_concurrent_positions"]:
        raise RiskViolation(
            f"concurrent positions {open_position_count} at max {config['max_concurrent_positions']}"
        )

    # Liquidation price check: liq must be >= 2x SL distance from entry
    # For long: liq ≈ entry * (1 - 1/leverage)
    # For short: liq ≈ entry * (1 + 1/leverage)
    if direction == "long":
        liq_price = entry * (1 - 1 / leverage)
        liq_dist = (entry - liq_price) / entry
    else:
        liq_price = entry * (1 + 1 / leverage)
        liq_dist = (liq_price - entry) / entry

    if liq_dist < 2 * sl_dist:
        raise RiskViolation(
            f"liquidation distance {liq_dist:.4%} must be >= 2x stop loss distance {sl_dist:.4%}"
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_risk_gate.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add risk/gate.py tests/test_risk_gate.py
git commit -m "feat: risk gate — order validation with 7 hard rules"
```

---

### Task 4: Market Stub (`market/stub.py`)

**Files:**
- Create: `market/stub.py`

**Interfaces:**
- Produces:
  - `get_market_state(assets: list[str]) -> dict` — returns deterministic market data for all requested assets

Return shape:
```python
{
  "SOL-PERP": {
    "ohlcv_15m": [[timestamp_ms, open, high, low, close, volume], ...],  # 40 candles
    "ohlcv_1h": [[ts, o, h, l, c, v], ...],   # 20 candles
    "ohlcv_4h": [[ts, o, h, l, c, v], ...],   # 10 candles
    "mid_price": 145.20,
    "bid": 145.18,
    "ask": 145.22,
    "funding_rate_current": -0.0042,
    "funding_rate_8h_history": [-0.0038, -0.0041, -0.0042],
    "open_interest_usd": 420_000_000,
    "open_interest_24h_change_pct": -3.2,
    "liquidation_volume_1h_usd": 8_500_000,
    "liquidation_direction_dominant": "long",
  },
  ...
}
```

- [ ] **Step 1: Write `market/stub.py`**

No tests needed for the stub — it's deterministic test infrastructure itself.

```python
"""Hardcoded deterministic market data for skeleton testing."""
import time

# Reference prices for each asset (close to real prices as of mid-2026)
_PRICES = {
    "BTC-PERP":   65_000.0,
    "ETH-PERP":    3_500.0,
    "SOL-PERP":      145.2,
    "BNB-PERP":      580.0,
    "XRP-PERP":        0.52,
    "DOGE-PERP":      0.12,
    "AVAX-PERP":      38.0,
    "LINK-PERP":      14.5,
    "ARB-PERP":        1.05,
    "OP-PERP":         2.40,
    "SUI-PERP":        1.80,
    "TON-PERP":        7.20,
    "PEPE-PERP":  0.0000142,
    "WIF-PERP":        2.10,
    "TRUMP-PERP":     12.50,
}

_FUNDING = {
    "BTC-PERP":  0.0001,
    "ETH-PERP":  0.0002,
    "SOL-PERP": -0.0042,   # negative — short pressure
    "BNB-PERP":  0.0003,
    "XRP-PERP": -0.0015,
    "DOGE-PERP": 0.0005,
    "AVAX-PERP": 0.0001,
    "LINK-PERP": 0.0002,
    "ARB-PERP":  0.0003,
    "OP-PERP":   0.0001,
    "SUI-PERP":  0.0008,
    "TON-PERP":  0.0002,
    "PEPE-PERP": 0.0010,
    "WIF-PERP":  0.0015,
    "TRUMP-PERP": -0.0020,
}


def _make_candles(price: float, n: int, interval_seconds: int) -> list:
    now_ms = int(time.time() * 1000)
    candles = []
    for i in range(n - 1, -1, -1):
        ts = now_ms - i * interval_seconds * 1000
        # Simulate mild oscillation around reference price
        offset = price * 0.002 * ((i % 5) - 2)
        o = price + offset
        h = o * 1.003
        l = o * 0.997
        c = o + price * 0.001
        v = price * 500
        candles.append([ts, round(o, 6), round(h, 6), round(l, 6), round(c, 6), round(v, 2)])
    return candles


def get_market_state(assets: list[str]) -> dict:
    state = {}
    for asset in assets:
        price = _PRICES.get(asset, 100.0)
        funding = _FUNDING.get(asset, 0.0001)
        spread = price * 0.0001
        state[asset] = {
            "ohlcv_15m": _make_candles(price, 40, 900),
            "ohlcv_1h": _make_candles(price, 20, 3600),
            "ohlcv_4h": _make_candles(price, 10, 14400),
            "mid_price": price,
            "bid": round(price - spread, 6),
            "ask": round(price + spread, 6),
            "funding_rate_current": funding,
            "funding_rate_8h_history": [funding * 0.9, funding * 0.95, funding],
            "open_interest_usd": 420_000_000,
            "open_interest_24h_change_pct": -3.2,
            "liquidation_volume_1h_usd": 8_500_000,
            "liquidation_direction_dominant": "long",
        }
    return state
```

- [ ] **Step 2: Commit**

```bash
git add market/stub.py
git commit -m "feat: market stub — deterministic OHLCV and funding data for all 15 assets"
```

---

### Task 5: Trading Bridge + Paper Bridge

**Files:**
- Create: `execution/bridge.py`
- Create: `execution/paper_bridge.py`
- Create: `tests/test_paper_bridge.py`

**Interfaces:**
- Consumes: `store/db.py` (all CRUD helpers), `market/stub.py` (for fill price)
- Produces:
  - `class TradingBridge(ABC)` with abstract methods `enter`, `get_positions`, `close`, `get_account`
  - `class PaperBridge(TradingBridge)` with concrete implementations
  - `PaperBridge(agent_id, conn, market_state)` constructor

`enter(order: dict) -> dict` — returns fill dict:
```python
{"trade_id": str, "fill_price": float, "notional_usd": float, "timestamp": str}
```

`get_positions() -> list[dict]` — returns position dicts from DB

`close(position_id: str, reason: str) -> dict` — returns close fill dict

`get_account() -> dict` — returns `{"balance": float, "peak": float}`

- [ ] **Step 1: Write `execution/bridge.py`**

```python
from abc import ABC, abstractmethod


class TradingBridge(ABC):
    @abstractmethod
    def enter(self, order: dict) -> dict: ...

    @abstractmethod
    def get_positions(self) -> list[dict]: ...

    @abstractmethod
    def close(self, position_id: str, reason: str) -> dict: ...

    @abstractmethod
    def get_account(self) -> dict: ...
```

- [ ] **Step 2: Write failing tests in `tests/test_paper_bridge.py`**

```python
import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from execution.paper_bridge import PaperBridge

AGENT_ID = "jade_hawk"
MARKET_STATE = {
    "SOL-PERP": {
        "mid_price": 145.20,
        "bid": 145.18,
        "ask": 145.22,
    }
}

ORDER = {
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
}


@pytest.fixture
def bridge(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    return PaperBridge(agent_id=AGENT_ID, conn=conn, market_state=MARKET_STATE)


def test_enter_creates_trade_record(bridge, conn):
    fill = bridge.enter(ORDER)
    assert fill["fill_price"] == pytest.approx(145.20, abs=0.01)
    assert "trade_id" in fill

    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1
    assert trades[0]["status"] == "open"
    assert trades[0]["asset"] == "SOL-PERP"


def test_enter_creates_position_record(bridge, conn):
    bridge.enter(ORDER)
    positions = bridge.get_positions()
    assert len(positions) == 1
    assert positions[0]["asset"] == "SOL-PERP"


def test_enter_debits_account(bridge, conn):
    bridge.enter(ORDER)
    account = bridge.get_account()
    # 10% of 50000 = 5000 notional; balance should reflect open position
    # For M1 we track notional as "reserved" — balance unchanged until close
    assert account["balance"] == pytest.approx(50000.0, abs=1.0)


def test_close_removes_position(bridge, conn):
    fill = bridge.enter(ORDER)
    positions_before = bridge.get_positions()
    assert len(positions_before) == 1
    pos_id = positions_before[0]["id"]
    bridge.close(pos_id, "take_profit")
    assert bridge.get_positions() == []


def test_close_marks_trade_closed(bridge, conn):
    bridge.enter(ORDER)
    pos_id = bridge.get_positions()[0]["id"]
    bridge.close(pos_id, "take_profit")
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert trades[0]["status"] == "closed"
    assert trades[0]["exit_reason"] == "take_profit"
```

- [ ] **Step 3: Run to confirm failure**

```bash
pytest tests/test_paper_bridge.py -v
```

Expected: `ImportError` — `execution.paper_bridge` not found.

- [ ] **Step 4: Implement `execution/paper_bridge.py`**

```python
import json
from datetime import datetime, timezone
from store.db import (
    get_trades, insert_trade, insert_position, get_positions,
    delete_position, insert_account_snapshot, get_latest_account,
)
from execution.bridge import TradingBridge


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trade_id(agent_id: str, asset: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = asset.replace("-PERP", "")
    return f"{agent_id}_{ts}_{short}"


class PaperBridge(TradingBridge):
    def __init__(self, agent_id: str, conn, market_state: dict):
        self.agent_id = agent_id
        self.conn = conn
        self.market_state = market_state

    def enter(self, order: dict) -> dict:
        asset = order["asset"]
        market = self.market_state.get(asset, {})
        fill_price = market.get("mid_price", order["entry_price"])

        account = self.get_account()
        balance = account["balance"]
        notional = balance * order["position_size_pct"]

        trade_id = _trade_id(self.agent_id, asset)
        pos_id = f"pos_{trade_id}"
        now = _now()

        trade = {
            "id": trade_id,
            "agent_id": self.agent_id,
            "thesis_version": 1,
            "account_balance_at_entry": balance,
            "mode": "paper",
            "asset": asset,
            "direction": order["direction"],
            "entry_price": fill_price,
            "stop_loss_price": order["stop_loss_price"],
            "take_profit_price": order["take_profit_price"],
            "leverage": order["leverage"],
            "position_size_pct": order["position_size_pct"],
            "notional_usd": notional,
            "entry_timestamp": now,
            "status": "open",
        }
        insert_trade(self.conn, trade)

        position = {
            "id": pos_id,
            "agent_id": self.agent_id,
            "asset": asset,
            "direction": order["direction"],
            "entry_price": fill_price,
            "stop_loss_price": order["stop_loss_price"],
            "take_profit_price": order["take_profit_price"],
            "leverage": order["leverage"],
            "position_size_pct": order["position_size_pct"],
            "notional_usd": notional,
            "opened_at": now,
            "mode": "paper",
            "trade_id": trade_id,
        }
        insert_position(self.conn, position)

        return {"trade_id": trade_id, "fill_price": fill_price,
                "notional_usd": notional, "timestamp": now}

    def get_positions(self) -> list[dict]:
        return get_positions(self.conn, self.agent_id)

    def close(self, position_id: str, reason: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        if not row:
            return {}
        pos = dict(row)

        asset = pos["asset"]
        market = self.market_state.get(asset, {})
        exit_price = market.get("mid_price", pos["entry_price"])

        entry = pos["entry_price"]
        if pos["direction"] == "long":
            pnl_pct = (exit_price - entry) / entry
        else:
            pnl_pct = (entry - exit_price) / entry
        pnl_pct *= pos["leverage"]
        pnl_usd = pos["notional_usd"] * pnl_pct

        now = _now()
        self.conn.execute(
            """UPDATE trades SET status='closed', exit_price=?, exit_timestamp=?,
               exit_reason=?, pnl_pct=?, pnl_usd=?,
               result=? WHERE id=?""",
            (exit_price, now, reason, pnl_pct, pnl_usd,
             "win" if pnl_pct > 0 else "loss", pos["trade_id"]),
        )
        self.conn.commit()
        delete_position(self.conn, position_id)

        account = self.get_account()
        new_balance = account["balance"] + pnl_usd
        peak = max(account["peak"], new_balance)
        insert_account_snapshot(self.conn, self.agent_id, "paper", new_balance, peak)

        return {"exit_price": exit_price, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

    def get_account(self) -> dict:
        latest = get_latest_account(self.conn, self.agent_id, "paper")
        if latest:
            return {"balance": latest["balance"], "peak": latest["peak_balance"]}
        return {"balance": 50000.0, "peak": 50000.0}
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
pytest tests/test_paper_bridge.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add execution/bridge.py execution/paper_bridge.py tests/test_paper_bridge.py
git commit -m "feat: paper bridge — simulates fills at stub price, writes trades and positions to SQLite"
```

---

### Task 6: Persona Builder + Stub LLM

**Files:**
- Create: `agents/persona.py`
- Create: `llm/stub.py`

**Interfaces:**
- Produces:
  - `build_system_prompt(agent_name: str, config: dict) -> str`
  - `decide(system_prompt: str, decision_prompt: str) -> dict` — returns action dict

Stub `decide` always returns:
```python
{
  "action": "enter",
  "asset": "SOL-PERP",
  "direction": "long",
  "entry_price": 145.20,
  "stop_loss_price": 143.00,
  "take_profit_price": 152.00,
  "leverage": 3,
  "position_size_pct": 0.10,
  "hypothesis": "SOL funding has been negative for 3 consecutive 8h periods...",
  "key_conditions_met": ["persistent_negative_funding", "support_hold_15m"],
  "key_conditions_missing": [],
  "confidence": 0.65,
  "expected_value": "+1.0% EV: 62% win rate × 4.7% TP − 38% × 2.5% SL"
}
```

- [ ] **Step 1: Write `agents/persona.py`**

```python
def build_system_prompt(agent_name: str, config: dict) -> str:
    return f"""You are a professional discretionary trader at Forge, a quantitative prop \
trading firm trading crypto perpetuals. Your name is {agent_name}. \
Your account is ${config['desk']['starting_balance']:,.0f}. You keep it all and grow it — or you get cut.

Your edge is your thesis: a specific, well-reasoned hypothesis about a \
market inefficiency you can exploit reliably across varying conditions. \
You built it. You own it. You update it when the evidence demands.

You are evaluated on:
  Win rate (target: >55%)
  Profit factor (target: >1.4)
  Avg win / avg loss (target: >1.2)
  Weekly return (target: positive)
  Max drawdown (hard limit: 15%)
  Sharpe ratio (target: >1.5)
  Trade frequency (target: 3–15 per day)

You think in expected value. You do not overtrade. You do not take trades \
that don't fit your thesis. You have one job: find your edge, express it cleanly, \
and let it compound.

Output JSON only. No prose outside of JSON."""
```

- [ ] **Step 2: Write `llm/stub.py`**

```python
"""Stub LLM — returns a hardcoded valid SOL long trade. No network calls."""

_STUB_RESPONSE = {
    "action": "enter",
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
    "hypothesis": (
        "SOL funding has been negative for 3 consecutive 8h periods indicating sustained "
        "short pressure. Long liquidations in the last hour suggest a squeeze setup as "
        "trapped shorts face escalating cost. Price has held the 145 level on two 15m retests."
    ),
    "key_conditions_met": ["persistent_negative_funding", "support_hold_15m"],
    "key_conditions_missing": [],
    "confidence": 0.65,
    "expected_value": "+1.0% EV: 62% win rate × 4.7% TP − 38% × 2.5% SL",
}


def decide(system_prompt: str, decision_prompt: str) -> dict:
    return dict(_STUB_RESPONSE)
```

- [ ] **Step 3: Commit**

```bash
git add agents/persona.py llm/stub.py
git commit -m "feat: persona builder and stub LLM returning hardcoded SOL long"
```

---

### Task 7: Prompt Builder (`agents/prompt_builder.py`)

**Files:**
- Create: `agents/prompt_builder.py`

**Interfaces:**
- Consumes: `store/db.py` (get_trades, get_positions, get_latest_account), `market/stub.py` (get_market_state)
- Produces: `build_decision_prompt(agent_id: str, thesis_text: str, market_state: dict, conn) -> str`

- [ ] **Step 1: Write `agents/prompt_builder.py`**

```python
import json
from store.db import get_trades, get_positions, get_latest_account


def build_decision_prompt(agent_id: str, thesis_text: str,
                          market_state: dict, conn) -> str:
    account = get_latest_account(conn, agent_id, "paper") or {"balance": 50000.0, "peak_balance": 50000.0}
    balance = account["balance"]
    peak = account["peak_balance"]
    dd_pct = (peak - balance) / peak if peak > 0 else 0.0

    closed_trades = get_trades(conn, agent_id, limit=10)
    wins = [t for t in closed_trades if t.get("result") == "win"]
    losses = [t for t in closed_trades if t.get("result") == "loss"]
    win_rate = len(wins) / len(closed_trades) if closed_trades else 0.0

    open_positions = get_positions(conn, agent_id)

    # Format last 10 closed trades
    trade_lines = []
    for t in closed_trades:
        if t.get("status") == "closed":
            pnl = t.get("pnl_pct", 0) or 0
            trade_lines.append(
                f"  {t['asset']} {t['direction']} | PnL: {pnl:+.2%} | "
                f"exit: {t.get('exit_reason', '?')}"
            )
    trades_section = "\n".join(trade_lines) if trade_lines else "  No closed trades yet."

    # Format open positions
    pos_lines = []
    for p in open_positions:
        pos_lines.append(
            f"  {p['asset']} {p['direction']} @ {p['entry_price']:.4f} | "
            f"SL: {p['stop_loss_price']:.4f} | TP: {p['take_profit_price']:.4f}"
        )
    positions_section = "\n".join(pos_lines) if pos_lines else "  No open positions."

    # Format market state summary (top 5 assets by absolute funding rate)
    sorted_assets = sorted(
        market_state.items(),
        key=lambda kv: abs(kv[1].get("funding_rate_current", 0)),
        reverse=True,
    )[:5]
    market_lines = []
    for asset, data in sorted_assets:
        market_lines.append(
            f"  {asset:12s} price={data['mid_price']:.4f} "
            f"funding={data['funding_rate_current']:+.4%} "
            f"OI_24h_chg={data['open_interest_24h_change_pct']:+.1f}%"
        )
    market_section = "\n".join(market_lines)

    return f"""=== YOUR THESIS ===
{thesis_text}

=== PERFORMANCE SUMMARY ===
Account: ${balance:,.2f} | Peak: ${peak:,.2f} | Current DD: {dd_pct:.1%}
Closed trades: {len(closed_trades)} | Win rate: {win_rate:.0%}

=== LAST 10 CLOSED TRADES ===
{trades_section}

=== YOUR OPEN POSITIONS ===
{positions_section}

=== MARKET STATE (top 5 by funding magnitude) ===
{market_section}

=== DECISION ===
Based on your thesis, your performance record, and current market conditions, make a decision.
You may:
  - Enter a new trade: {{"action": "enter", "asset": "...", "direction": "long|short", \
"entry_price": 0.0, "stop_loss_price": 0.0, "take_profit_price": 0.0, \
"leverage": 1, "position_size_pct": 0.10, "hypothesis": "...", \
"key_conditions_met": [], "key_conditions_missing": [], "confidence": 0.0, "expected_value": "..."}}
  - Wait: {{"action": "wait", "reason": "..."}}
  - Close a position: {{"action": "close", "position_id": "...", "reason": "..."}}

Output JSON only."""
```

- [ ] **Step 2: Commit**

```bash
git add agents/prompt_builder.py
git commit -m "feat: prompt builder — assembles decision prompt from thesis, performance, market state"
```

---

### Task 8: Decision Loop (`agents/decision_loop.py`)

**Files:**
- Create: `agents/decision_loop.py`
- Create: `tests/test_decision_loop.py`

**Interfaces:**
- Consumes: `market/stub.py`, `agents/persona.py`, `agents/prompt_builder.py`, `llm/stub.py`, `risk/gate.py`, `execution/paper_bridge.py`, `store/db.py`
- Produces: `run_decision(agent_id, thesis_text, config, conn, get_market_fn, llm_fn, bridge_factory) -> dict`

`run_decision` returns:
```python
{"action": "enter"|"wait"|"close"|"risk_blocked", "detail": str}
```

`get_market_fn` signature: `(assets: list[str]) -> dict`
`llm_fn` signature: `(system_prompt: str, decision_prompt: str) -> dict`
`bridge_factory` signature: `(agent_id: str, conn, market_state: dict) -> TradingBridge`

- [ ] **Step 1: Write failing tests in `tests/test_decision_loop.py`**

```python
import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from agents.decision_loop import run_decision
from market.stub import get_market_state
from llm.stub import decide
from execution.paper_bridge import PaperBridge

AGENT_ID = "jade_hawk"
THESIS = "Funding rate mean reversion: persistent negative funding signals short squeeze."
CONFIG = {
    "universe": ["SOL-PERP"],
    "desk": {
        "starting_balance": 50000.0,
        "max_leverage": 10,
        "max_position_size_pct": 0.20,
        "max_concurrent_positions": 3,
        "drawdown_kill_pct": 0.15,
    },
}


def bridge_factory(agent_id, conn, market_state):
    return PaperBridge(agent_id=agent_id, conn=conn, market_state=market_state)


def test_decision_loop_enter_creates_trade(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    result = run_decision(
        agent_id=AGENT_ID,
        thesis_text=THESIS,
        config=CONFIG,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=decide,
        bridge_factory=bridge_factory,
    )

    assert result["action"] == "enter"
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1


def test_decision_loop_risk_block_does_not_create_trade(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    # LLM that returns an invalid order (leverage over cap)
    def bad_llm(sys, prompt):
        return {
            "action": "enter",
            "asset": "SOL-PERP",
            "direction": "long",
            "entry_price": 145.20,
            "stop_loss_price": 143.00,
            "take_profit_price": 152.00,
            "leverage": 15,  # over cap
            "position_size_pct": 0.10,
            "hypothesis": "test",
            "key_conditions_met": [],
            "key_conditions_missing": [],
            "confidence": 0.5,
            "expected_value": "test",
        }

    result = run_decision(
        agent_id=AGENT_ID,
        thesis_text=THESIS,
        config=CONFIG,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=bad_llm,
        bridge_factory=bridge_factory,
    )

    assert result["action"] == "risk_blocked"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []


def test_decision_loop_wait_does_not_create_trade(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt):
        return {"action": "wait", "reason": "no setup fits thesis today"}

    result = run_decision(
        agent_id=AGENT_ID,
        thesis_text=THESIS,
        config=CONFIG,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=wait_llm,
        bridge_factory=bridge_factory,
    )

    assert result["action"] == "wait"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_decision_loop.py -v
```

Expected: `ImportError` — `agents.decision_loop` not found.

- [ ] **Step 3: Implement `agents/decision_loop.py`**

```python
import logging
from agents.persona import build_system_prompt
from agents.prompt_builder import build_decision_prompt
from risk.gate import validate_order, RiskViolation
from store.db import get_positions

logger = logging.getLogger(__name__)


def run_decision(agent_id: str, thesis_text: str, config: dict, conn,
                 get_market_fn, llm_fn, bridge_factory) -> dict:
    """
    Full decision cycle for one agent wake.
    Returns {"action": str, "detail": str}.
    Never raises — all exceptions are caught and logged.
    """
    try:
        assets = config["universe"]
        desk_config = config["desk"]

        # 1. Fetch market state
        market_state = get_market_fn(assets)

        # 2. Build prompts
        system_prompt = build_system_prompt(agent_id, config)
        decision_prompt = build_decision_prompt(agent_id, thesis_text, market_state, conn)

        # 3. Call LLM
        response = llm_fn(system_prompt, decision_prompt)

        action = response.get("action", "wait")

        if action == "wait":
            logger.info("[%s] LLM decided to wait: %s", agent_id, response.get("reason", ""))
            return {"action": "wait", "detail": response.get("reason", "")}

        if action == "close":
            pos_id = response.get("position_id")
            reason = response.get("reason", "agent_close")
            bridge = bridge_factory(agent_id, conn, market_state)
            fill = bridge.close(pos_id, reason)
            logger.info("[%s] Closed position %s: %s", agent_id, pos_id, fill)
            return {"action": "close", "detail": str(fill)}

        if action == "enter":
            # 4. Risk gate
            open_positions = get_positions(conn, agent_id)
            try:
                validate_order(
                    order=response,
                    account_balance=_get_balance(conn, agent_id),
                    config=desk_config,
                    open_position_count=len(open_positions),
                )
            except RiskViolation as e:
                logger.warning("[%s] Risk gate blocked order: %s", agent_id, e.reason)
                return {"action": "risk_blocked", "detail": e.reason}

            # 5. Execute via paper bridge
            bridge = bridge_factory(agent_id, conn, market_state)
            fill = bridge.enter(response)
            logger.info("[%s] Entered trade: %s", agent_id, fill)
            return {"action": "enter", "detail": str(fill)}

        logger.warning("[%s] Unknown action: %s", agent_id, action)
        return {"action": "unknown", "detail": str(response)}

    except Exception as exc:
        logger.error("[%s] Decision loop error: %s", agent_id, exc, exc_info=True)
        return {"action": "error", "detail": str(exc)}


def _get_balance(conn, agent_id: str) -> float:
    from store.db import get_latest_account
    latest = get_latest_account(conn, agent_id, "paper")
    return latest["balance"] if latest else 50000.0
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_decision_loop.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/decision_loop.py tests/test_decision_loop.py
git commit -m "feat: decision loop — market fetch, prompt build, LLM call, risk gate, paper execute"
```

---

### Task 9: Agent Runtime + Initial Thesis

**Files:**
- Create: `agents/runtime.py`
- Create: `agents/theses/jade_hawk_v1.md`

**Interfaces:**
- Consumes: `agents/decision_loop.run_decision`
- Produces:
  - `class AgentRuntime` with `__init__(agent_id, thesis_path, config, conn, get_market_fn, llm_fn, bridge_factory)` and `async def tick() -> None`

`tick()` is the APScheduler job — runs one decision cycle, catches all exceptions.

- [ ] **Step 1: Write `agents/theses/jade_hawk_v1.md`**

```markdown
# jade_hawk — Thesis v1: Funding Rate Mean Reversion

## Edge Hypothesis

Crypto perpetual markets generate funding rates that periodically diverge far from
equilibrium due to one-sided speculative positioning. When funding rates stay persistently
negative (longs pay shorts, meaning market is net short), mechanical squeeze pressure builds:
shorts pay funding each 8-hour period, reducing their risk-adjusted return. Once that cost
becomes sufficiently high, short covering accelerates, driving price up even without a
fundamental catalyst.

**Primary signal:** Funding rate negative for 3+ consecutive 8-hour periods on an asset
in my universe, with current rate ≤ -0.03%.

## Entry Conditions

**Required (all must be met):**
1. Funding rate ≤ -0.03% for current period
2. Funding rate was also negative in at least 2 of the prior 3 periods (persistence check)
3. Price has not already rallied >3% in the last 4h (squeeze not already underway)
4. Open interest has not fallen >10% in 24h (capitulation already complete = no squeeze fuel)

**Supporting (raise confidence, not required):**
- Recent long liquidation volume > $5M/h (trapped longs being cleaned out = cleaner setup)
- BTC dominance stable or falling (risk-on environment favors the trade)
- Asset is near a 15m support level

## Position Parameters

- Direction: Long always (fade the short squeeze)
- Leverage: 3x (low leverage for squeeze trades — timing is imprecise)
- Position size: 10% of account per trade
- Stop loss: 2.0% below entry price
- Take profit: 4.5% above entry price (2.25:1 reward/risk)
- Max hold time: 8 hours (if TP not hit, evaluate at funding reset)

## Known Weaknesses

- In persistent trending bear markets, negative funding can remain negative for weeks
  without triggering a squeeze — this thesis underperforms in `trending_bear` regime
- Works best in `range_high_vol` and `trending_bull` regimes
- Timing risk: squeeze can take 2-12 hours to materialize; overnight gaps can hit SL

## Assets in Focus

Primary: SOL, ETH, ARB, OP (mid-cap perps with meaningful funding volatility)
Secondary: BTC (lower funding variance but high liquidity)
Avoid: PEPE, WIF, DOGE (too noisy, funding spikes don't mean squeeze)
```

- [ ] **Step 2: Write `agents/runtime.py`**

```python
import logging
from pathlib import Path
from agents.decision_loop import run_decision

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(self, agent_id: str, thesis_path: str, config: dict, conn,
                 get_market_fn, llm_fn, bridge_factory):
        self.agent_id = agent_id
        self.thesis_path = Path(thesis_path)
        self.config = config
        self.conn = conn
        self.get_market_fn = get_market_fn
        self.llm_fn = llm_fn
        self.bridge_factory = bridge_factory

    def _load_thesis(self) -> str:
        try:
            return self.thesis_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("[%s] Thesis file not found: %s", self.agent_id, self.thesis_path)
            return "No thesis loaded."

    async def tick(self) -> None:
        """Called by APScheduler on each wake interval. Never raises."""
        logger.info("[%s] Waking up", self.agent_id)
        try:
            thesis_text = self._load_thesis()
            result = run_decision(
                agent_id=self.agent_id,
                thesis_text=thesis_text,
                config=self.config,
                conn=self.conn,
                get_market_fn=self.get_market_fn,
                llm_fn=self.llm_fn,
                bridge_factory=self.bridge_factory,
            )
            logger.info("[%s] Decision: %s — %s",
                        self.agent_id, result["action"], result.get("detail", ""))
        except Exception as exc:
            logger.error("[%s] Unexpected tick error: %s", self.agent_id, exc, exc_info=True)
```

- [ ] **Step 3: Commit**

```bash
git add agents/runtime.py agents/theses/jade_hawk_v1.md
git commit -m "feat: agent runtime (APScheduler tick), jade_hawk funding squeeze thesis"
```

---

### Task 10: Web UI (`web/app.py` + templates)

**Files:**
- Create: `web/app.py`
- Create: `web/templates/base.html`
- Create: `web/templates/overview.html`

**Interfaces:**
- Consumes: `store/db.py` (get_trades, get_agent, get_latest_account, get_positions)
- Produces: FastAPI app object exported as `app`

The `GET /` route renders `overview.html` showing: agent name, paper balance, last 10 trades.

- [ ] **Step 1: Write `web/app.py`**

```python
import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app = FastAPI(title="Forge")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _get_db():
    """Get DB connection from app state — set on startup by forge.py."""
    return app.state.conn


@app.get("/")
async def overview(request: Request):
    conn = _get_db()
    agent_id = "jade_hawk"
    agent = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    account = conn.execute(
        "SELECT * FROM accounts WHERE agent_id = ? AND mode = 'paper' ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    trades = conn.execute(
        "SELECT * FROM trades WHERE agent_id = ? ORDER BY entry_timestamp DESC LIMIT 10",
        (agent_id,),
    ).fetchall()
    positions = conn.execute(
        "SELECT * FROM positions WHERE agent_id = ?", (agent_id,)
    ).fetchall()

    return templates.TemplateResponse("overview.html", {
        "request": request,
        "agent": dict(agent) if agent else {},
        "account": dict(account) if account else {"balance": 50000.0, "peak_balance": 50000.0},
        "trades": [dict(t) for t in trades],
        "positions": [dict(p) for p in positions],
    })


@app.get("/health")
async def health():
    conn = _get_db()
    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return {"status": "ok", "agents": agent_count, "trades": trade_count}
```

- [ ] **Step 2: Write `web/templates/base.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}Forge{% endblock %}</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Courier New', monospace; background: #0d1117; color: #e6edf3; padding: 20px; }
    h1 { color: #f0883e; margin-bottom: 16px; }
    h2 { color: #58a6ff; margin: 20px 0 10px; }
    table { width: 100%; border-collapse: collapse; margin-top: 8px; }
    th { background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px; font-size: 12px; }
    td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 13px; }
    tr:hover td { background: #161b22; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; }
    .badge-rookie { background: #238636; }
    .badge-active { background: #1f6feb; }
    .badge-suspended { background: #da3633; }
    .win { color: #3fb950; }
    .loss { color: #f85149; }
    .open { color: #f0883e; }
    .stat-card { display: inline-block; background: #161b22; border: 1px solid #30363d;
                 border-radius: 6px; padding: 12px 20px; margin: 8px 8px 8px 0; min-width: 140px; }
    .stat-label { color: #8b949e; font-size: 11px; }
    .stat-value { color: #e6edf3; font-size: 20px; font-weight: bold; margin-top: 4px; }
    nav { margin-bottom: 24px; }
    nav a { color: #58a6ff; text-decoration: none; margin-right: 16px; }
    nav a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>⚒ Forge — Prop Trading Desk</h1>
  <nav>
    <a href="/">Overview</a>
    <a href="/health">Health</a>
  </nav>
  {% block content %}{% endblock %}
</body>
</html>
```

- [ ] **Step 3: Write `web/templates/overview.html`**

```html
{% extends "base.html" %}
{% block title %}Forge — Desk Overview{% endblock %}
{% block content %}

<h2>Agent: {{ agent.get('name', 'jade_hawk') }}
  <span class="badge badge-{{ agent.get('status', 'rookie') }}">
    {{ agent.get('status', 'ROOKIE').upper() }}
  </span>
</h2>

<div>
  <div class="stat-card">
    <div class="stat-label">Paper Balance</div>
    <div class="stat-value">${{ "{:,.2f}".format(account.get('balance', 50000)) }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Peak Balance</div>
    <div class="stat-value">${{ "{:,.2f}".format(account.get('peak_balance', 50000)) }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Open Positions</div>
    <div class="stat-value">{{ positions | length }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total Trades</div>
    <div class="stat-value">{{ trades | length }}</div>
  </div>
</div>

{% if positions %}
<h2>Open Positions</h2>
<table>
  <tr><th>Asset</th><th>Direction</th><th>Entry</th><th>SL</th><th>TP</th><th>Size</th><th>Opened</th></tr>
  {% for p in positions %}
  <tr>
    <td>{{ p.asset }}</td>
    <td class="{{ 'win' if p.direction == 'long' else 'loss' }}">{{ p.direction.upper() }}</td>
    <td>{{ "{:.4f}".format(p.entry_price) }}</td>
    <td>{{ "{:.4f}".format(p.stop_loss_price) }}</td>
    <td>{{ "{:.4f}".format(p.take_profit_price) }}</td>
    <td>{{ "{:.0%}".format(p.position_size_pct) }}</td>
    <td>{{ p.opened_at[:19] if p.opened_at else '' }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}

<h2>Last 10 Trades</h2>
{% if trades %}
<table>
  <tr><th>ID</th><th>Asset</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL%</th><th>Status</th><th>Time</th></tr>
  {% for t in trades %}
  <tr>
    <td style="font-size:11px;color:#8b949e">{{ t.id[-20:] if t.id else '' }}</td>
    <td>{{ t.asset }}</td>
    <td class="{{ 'win' if t.direction == 'long' else 'loss' }}">{{ t.direction[0].upper() if t.direction else '' }}</td>
    <td>{{ "{:.4f}".format(t.entry_price) if t.entry_price else '—' }}</td>
    <td>{{ "{:.4f}".format(t.exit_price) if t.exit_price else '—' }}</td>
    <td class="{{ 'win' if (t.pnl_pct or 0) > 0 else ('loss' if (t.pnl_pct or 0) < 0 else '') }}">
      {{ "{:+.2%}".format(t.pnl_pct) if t.pnl_pct is not none else '—' }}
    </td>
    <td class="{{ 'open' if t.status == 'open' else ('win' if t.result == 'win' else 'loss') }}">
      {{ t.status.upper() }}
    </td>
    <td style="font-size:11px">{{ t.entry_timestamp[:19] if t.entry_timestamp else '' }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p style="color:#8b949e; margin-top: 12px;">No trades yet. Agent wakes every 60 seconds.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Commit**

```bash
git add web/app.py web/templates/base.html web/templates/overview.html
git commit -m "feat: web UI — FastAPI overview page showing agent balance and trade history"
```

---

### Task 11: Main Entrypoint + GitHub Repo

**Files:**
- Create: `forge.py`
- Create: `README.md` (minimal)

- [ ] **Step 1: Write `forge.py`**

```python
"""
Forge — single entrypoint.
Starts the agent scheduler and web server in one process.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from store.db import get_connection, init_schema, insert_agent, insert_account_snapshot
from market.stub import get_market_state
from llm.stub import decide
from execution.paper_bridge import PaperBridge
from agents.runtime import AgentRuntime
from web.app import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forge")

DB_PATH = Path("data/forge.db")
CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def setup_agent(conn, agent_id: str, config: dict) -> None:
    """Create agent in DB if not already present."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    insert_agent(conn, agent_id, agent_id, now, "{}")
    # Only insert opening balance if no account row exists
    existing = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE agent_id = ? AND mode = 'paper'",
        (agent_id,),
    ).fetchone()[0]
    if existing == 0:
        balance = config["desk"]["starting_balance"]
        insert_account_snapshot(conn, agent_id, "paper", balance, balance)
    logger.info("Agent %s ready", agent_id)


def bridge_factory(agent_id: str, conn, market_state: dict) -> PaperBridge:
    return PaperBridge(agent_id=agent_id, conn=conn, market_state=market_state)


async def main():
    config = load_config()
    conn = get_connection(str(DB_PATH))
    init_schema(conn)

    agent_id = "jade_hawk"
    setup_agent(conn, agent_id, config)

    # Make DB connection available to web app
    web_app.state.conn = conn

    # Build agent runtime
    thesis_path = Path("agents/theses/jade_hawk_v1.md")
    runtime = AgentRuntime(
        agent_id=agent_id,
        thesis_path=str(thesis_path),
        config=config,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=decide,
        bridge_factory=bridge_factory,
    )

    # Schedule agent wakeups
    wake_seconds = config["desk"]["wake_interval_seconds"]
    scheduler = AsyncIOScheduler()
    scheduler.add_job(runtime.tick, "interval", seconds=wake_seconds, id=agent_id)
    scheduler.start()
    logger.info("Scheduler started — %s wakes every %ds", agent_id, wake_seconds)

    # Start web server
    server_config = uvicorn.Config(web_app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(server_config)
    logger.info("Web UI starting at http://localhost:8000")

    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Write minimal `README.md`**

```markdown
# Forge — Evolutionary Prop Trading System

An autonomous AI trader-agent ecosystem. Agents paper-trade crypto perpetuals,
evolve their strategies through thesis reflection, and compete for live capital.

## Milestone 1: Walking Skeleton

```bash
# Install
pip install -r requirements.txt

# Run
python forge.py
```

Open http://localhost:8000 to see jade_hawk trading.

## Requirements

- Python 3.11+
- (Milestone 2+) Ollama with `qwen3:35b` model
- (Milestone 2+) Hyperliquid API access
```

- [ ] **Step 3: Create GitHub repository**

```bash
gh repo create forge --public --description "Evolutionary AI prop trading system on crypto perpetuals" --source . --remote origin --push
```

If `gh repo create` fails because the directory already has commits, push manually:
```bash
git remote add origin https://github.com/G-R-A-V-I-T-Y/forge.git
git push -u origin main
```

- [ ] **Step 4: Final commit and push**

```bash
git add forge.py README.md
git commit -m "feat(M1): main entrypoint — scheduler + uvicorn in single asyncio process"
git push -u origin main
```

- [ ] **Step 5: Verify milestone 1 acceptance criteria**

```bash
python forge.py
```

Expected console output (within 5 minutes):
```
INFO forge: Agent jade_hawk ready
INFO forge: Scheduler started — jade_hawk wakes every 60s
INFO forge: Web UI starting at http://localhost:8000
INFO agents.runtime: [jade_hawk] Waking up
INFO agents.runtime: [jade_hawk] Decision: enter — ...
INFO agents.runtime: [jade_hawk] Waking up
INFO agents.runtime: [jade_hawk] Decision: enter — ...
```

Open `http://localhost:8000` — verify:
- Agent name and status displayed
- Paper balance shown at $50,000
- Trade rows appearing in the last 10 trades table (one per 60 seconds)

After 3 minutes, stop the process and verify SQLite:
```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/forge.db')
trades = conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
print(f'{trades} trades recorded')
assert trades >= 3, 'Need at least 3 trades for milestone completion'
print('Milestone 1 COMPLETE')
"
```

---

## Summary

After all tasks complete:
- `python forge.py` runs without error
- `jade_hawk` makes a stub trade decision every 60 seconds
- All trades recorded in `data/forge.db`
- `localhost:8000` shows live trade history
- 11 tasks → 11 commits, each independently reviewable
- Zero external dependencies in M1 (no Hyperliquid, no Ollama, no API keys)
