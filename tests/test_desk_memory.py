"""tests/test_desk_memory.py — M11.1 desk memory digest coverage."""
from __future__ import annotations

import pytest

from meta.desk_memory import get_desk_digest
from store.db import insert_agent

AGENT_ID = "alpha_viper"


def _insert_hypothesis(conn, agent_id, claim, status, effect_observed=None,
                       feature=None, direction=None, regime_context=None):
    conn.execute(
        """INSERT INTO hypotheses
               (agent_id, claim, status, effect_observed, feature,
                direction, regime_context, predicted_effect, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-07-01T00:00:00Z')""",
        (agent_id, claim, status, effect_observed, feature, direction,
         regime_context, f"predict_{claim[:20]}"),
    )
    conn.commit()


class TestGetDeskDigest:
    def test_empty_digest_when_no_hypotheses(self, conn):
        digest = get_desk_digest(conn)
        assert "VALIDATED HYPOTHESES (0)" in digest
        assert "FALSIFIED HYPOTHESES (0)" in digest
        assert "Total hypotheses tracked: 0" in digest

    def test_digest_includes_validated(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "funding drives reversion",
                           "validated", effect_observed=2.5,
                           feature="funding", direction="down",
                           regime_context="contango")
        digest = get_desk_digest(conn)
        assert "VALIDATED HYPOTHESES (1)" in digest
        assert "funding drives reversion" in digest
        assert "funding" in digest
        assert "+2.5" in digest

    def test_digest_includes_falsified(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "volume spike means breakout",
                           "falsified", effect_observed=-1.2,
                           feature="volume", direction="up")
        digest = get_desk_digest(conn)
        assert "FALSIFIED HYPOTHESES (1)" in digest
        assert "volume spike means breakout" in digest
        assert "-1.2" in digest

    def test_validated_ordered_by_effect_desc(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "small effect",
                           "validated", effect_observed=0.5)
        _insert_hypothesis(conn, AGENT_ID, "large effect",
                           "validated", effect_observed=5.0)
        _insert_hypothesis(conn, AGENT_ID, "medium effect",
                           "validated", effect_observed=2.0)
        digest = get_desk_digest(conn)
        large_pos = digest.index("large effect")
        med_pos = digest.index("medium effect")
        small_pos = digest.index("small effect")
        assert large_pos < med_pos < small_pos

    def test_falsified_ordered_by_effect_asc(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "mild miss",
                           "falsified", effect_observed=-0.5)
        _insert_hypothesis(conn, AGENT_ID, "big miss",
                           "falsified", effect_observed=-5.0)
        digest = get_desk_digest(conn)
        big_pos = digest.index("big miss")
        mild_pos = digest.index("mild miss")
        assert big_pos < mild_pos

    def test_max_items_limits_output(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        for i in range(10):
            _insert_hypothesis(conn, AGENT_ID, f"validated {i}",
                               "validated", effect_observed=float(i))
        digest = get_desk_digest(conn, max_items=4)
        assert "VALIDATED HYPOTHESES (2)" in digest

    def test_proposed_hypotheses_excluded(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "proposed claim",
                           "proposed", effect_observed=3.0)
        digest = get_desk_digest(conn)
        assert "VALIDATED HYPOTHESES (0)" in digest
        assert "proposed claim" not in digest


class TestDeskMemoryApi:
    def _client(self, conn, config=None):
        from fastapi.testclient import TestClient
        from web.app import app
        app.state.conn = conn
        app.state.provider = None
        app.state.config = config or {}
        return TestClient(app)

    def test_api_desk_memory_returns_digest(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "test claim",
                           "validated", effect_observed=1.0)
        r = self._client(conn).get("/api/desk-memory")
        assert r.status_code == 200
        data = r.json()
        assert "digest" in data
        assert "test claim" in data["digest"]
        assert "trial_count" in data
        assert isinstance(data["trial_count"], int)

    def test_api_desk_memory_empty_db(self, conn):
        r = self._client(conn).get("/api/desk-memory")
        assert r.status_code == 200
        data = r.json()
        assert "Total hypotheses tracked: 0" in data["digest"]

    def test_overview_includes_desk_digest(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        _insert_hypothesis(conn, AGENT_ID, "funding edge",
                           "validated", effect_observed=3.0)
        r = self._client(conn).get("/")
        assert r.status_code == 200
        assert "Desk Knowledge" in r.text
        assert "funding edge" in r.text

    def test_overview_includes_trial_count(self, conn):
        insert_agent(conn, AGENT_ID, AGENT_ID, "2026-01-01T00:00:00Z", "{}")
        r = self._client(conn).get("/")
        assert r.status_code == 200
        assert "Backtest Trials" in r.text
