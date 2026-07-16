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
    # T7 Gap 2: one generalized coverage surface -- forward-labeling
    # coverage is the headline, wait-counterfactual fill stats are folded
    # in as secondary stats within the SAME section (not a second panel).
    assert "Decision Coverage" in r.text
    assert "Wait-Counterfactual Fill" in r.text
    assert r.text.count("Coverage %") == 2  # one per sub-stat row, one section
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
    assert "Decision Coverage" in r.text


def test_decisions_page_single_coverage_context_var(conn):
    """web/app.py must pass one unified `coverage` context var to the
    template -- not the old two-panel `coverage` + `labeling_coverage`
    pair -- with the counterfactual stats nested inside it."""
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    conn.commit()

    captured = {}
    from web.app import templates

    orig_response = templates.TemplateResponse

    def _spy(name, context, *a, **kw):
        if name == "decisions.html":
            captured.update(context)
        return orig_response(name, context, *a, **kw)

    with patch.object(templates, "TemplateResponse", side_effect=_spy):
        app.state.conn = conn
        r = TestClient(app).get("/decisions")

    assert r.status_code == 200
    assert "coverage" in captured
    assert "labeling_coverage" not in captured
    assert "counterfactual" in captured["coverage"]
    assert "eligible_decisions" in captured["coverage"]  # labeling headline
    assert "filled" in captured["coverage"]["counterfactual"]
