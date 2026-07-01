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
    assert '<th class="sortable" data-sort="model"' in r.text


def test_overview_leaderboard_shows_no_model_available_badge(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    update_last_model_used(conn, AGENT_ID, "no model available")
    r = _client(conn).get("/")
    assert r.status_code == 200
    assert "NO MODEL AVAILABLE" in r.text


def test_overview_leaderboard_shows_em_dash_before_any_cycle(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    r = _client(conn).get("/")
    assert r.status_code == 200
    # em-dash is HTML-escaped in the raw response text ("&mdash;")
    assert "&mdash;" in r.text
