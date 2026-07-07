import sqlite3
import pytest
from pathlib import Path

from store.db import init_schema

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


@pytest.fixture(autouse=True)
def _isolate_ledger_dir(tmp_path, monkeypatch):
    """Redirect every test's ledger writes to a per-test tmp_path, never the
    real repo `ledger/` directory.

    store/ledger.py's append_ledger_record() is now reached indirectly by
    ordinary decision/trade/heartbeat code paths (agents/decision_loop.py's
    log_decision, store/positions.py's execute_close, market/heartbeat.py's
    export_heartbeat_to_ledger) -- without this, any test that exercises
    those paths without its own explicit ledger_dir override pollutes the
    actual working tree with real files on every `pytest` run. Individual
    tests that need to assert on ledger content still set
    store.ledger.LEDGER_DIR explicitly (which simply overrides this
    default for that test).
    """
    import store.ledger as ledger_module

    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema applied.

    check_same_thread=False because FastAPI's TestClient (used by
    tests/test_web_trades.py) runs the app in a worker thread.
    """
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    init_schema(c)
    yield c
    c.close()
