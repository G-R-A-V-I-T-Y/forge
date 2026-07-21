"""Tests for the overview leaderboard and /api/desk — confirms
last_model_used is exposed per agent (model-fallback-chain feature)."""
from fastapi.testclient import TestClient

from store.db import insert_agent, update_last_model_used
from web.app import app

AGENT_ID = "jade_hawk"


def _client(conn, config=None) -> TestClient:
    app.state.conn = conn
    app.state.provider = None
    app.state.config = config or {}
    return TestClient(app)


def test_api_desk_includes_last_model_used(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    update_last_model_used(conn, AGENT_ID, "DeepSeek V4 Flash Free")
    r = _client(conn).get("/api/desk")
    assert r.status_code == 200
    data = r.json()
    agent = next(a for a in data if a["name"] == AGENT_ID)
    assert agent["last_model_used"] == "DeepSeek V4 Flash Free"


def test_api_desk_last_model_used_null_before_any_cycle(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    r = _client(conn).get("/api/desk")
    data = r.json()
    agent = next(a for a in data if a["name"] == AGENT_ID)
    assert agent["last_model_used"] is None


def test_overview_leaderboard_shows_model_column(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    update_last_model_used(conn, AGENT_ID, "Big Pickle")
    r = _client(conn).get("/")
    assert r.status_code == 200
    assert "Big Pickle" in r.text
    # Server-rendered leaderboard table (plain HTML, not a client-side JS grid)
    assert '<a href="/agents/jade_hawk">jade_hawk</a>' in r.text


def test_overview_leaderboard_shows_no_model_available_badge(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    update_last_model_used(conn, AGENT_ID, "no model available")
    r = _client(conn).get("/")
    assert r.status_code == 200
    # LyteNyte Grid: model badge text is in the JS source
    assert "NO MODEL" in r.text


def test_overview_leaderboard_shows_em_dash_before_any_cycle(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    r = _client(conn).get("/")
    assert r.status_code == 200
    # Server-rendered leaderboard table: em-dash is the HTML entity &mdash;
    # (win_rate, profit_factor, sharpe all render as em-dash before any cycle)
    assert r.text.count("&mdash;") >= 3


def test_overview_leaderboard_shows_entry_disabled_badge(conn):
    """An open entry_disables row (any disabled_by) must be visible on the
    leaderboard with its reason -- this was previously invisible anywhere
    in the UI, which is exactly how the 2026-07-12..07-15 mass-block went
    unnoticed for 8 days."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        f"VALUES ('{AGENT_ID}', 'human', '2026-07-15T06:26:40Z', 'Entry blocked by risk check')"
    )
    conn.commit()
    r = _client(conn).get("/")
    assert r.status_code == 200
    assert "Entry blocked by risk check" in r.text


def test_overview_leaderboard_no_badge_when_gate_open(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    r = _client(conn).get("/")
    assert r.status_code == 200
    assert "ENTRY DISABLED" not in r.text
