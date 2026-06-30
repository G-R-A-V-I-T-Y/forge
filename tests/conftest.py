import sqlite3
import pytest
from pathlib import Path

from store.db import init_schema

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


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
