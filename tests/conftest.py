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
