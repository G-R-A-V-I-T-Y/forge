"""Tests for the /decisions page: counterfactual + M10 labeling coverage
tiles, per-agent decision list, and the hypotheses panel."""
from fastapi.testclient import TestClient

from store.db import insert_agent
from web.app import app


AGENT_ID = "jade_hawk"


def _client(conn) -> TestClient:
    app.state.conn = conn
    return TestClient(app)


def test_decisions_page_renders_with_coverage_tiles(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    conn.execute(
        """INSERT INTO decisions (agent_id, timestamp, decision_action, decision_reason)
           VALUES (?, ?, ?, ?)""",
        (AGENT_ID, "2026-07-01T00:00:00Z", "wait", "no setup"),
    )
    conn.commit()

    r = _client(conn).get("/decisions")
    assert r.status_code == 200
    # Both coverage panels render (counterfactual M6 + labeling M10).
    assert "Counterfactual Coverage" in r.text
    assert "Forward Labeling Coverage" in r.text
    # Decision content shows up.
    assert "no setup" in r.text


def test_decisions_page_hypotheses_panel_uses_claim_column(conn):
    """Regression: the hypotheses query used to select a non-existent
    `summary` column, silently swallowed by a bare except, so the panel
    always rendered empty. Verifies claim text now actually appears."""
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    conn.execute(
        """INSERT INTO hypotheses
           (agent_id, claim, predicted_effect, status, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (AGENT_ID, "high funding predicts mean reversion", "positive edge",
         "proposed", "2026-07-01T00:00:00Z"),
    )
    conn.commit()

    r = _client(conn).get("/decisions")
    assert r.status_code == 200
    assert "high funding predicts mean reversion" in r.text


def test_decisions_page_empty_desk(conn):
    r = _client(conn).get("/decisions")
    assert r.status_code == 200
    assert "Forward Labeling Coverage" in r.text
