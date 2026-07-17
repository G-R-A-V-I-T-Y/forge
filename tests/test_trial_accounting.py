"""tests/test_trial_accounting.py — M11.4 backtest trial recording coverage."""
from __future__ import annotations

import pytest

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.walk_forward import _spec_hash, record_trial
from datetime import datetime, timezone


def _spec(agent_id: str = "trial_agent") -> Spec:
    return Spec(
        agent_id=agent_id, spec_version=1, thesis_version=1,
        universe_include=["SOL-PERP"], regime_exclude=[],
        direction="long", confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[EvidenceTerm(
            name="funding_term", feature="funding",
            thresholds=[Threshold(op="<", weight=0.6, value=-0.001),
                        Threshold(op="else", weight=0.0)],
            missing="skip",
        )],
        secondary_evidence=[],
        stop_loss_pct=0.03, take_profit_pct=0.06,
        max_hold_hours=72, leverage=3, position_size_pct=0.10,
    )


class TestSpecHash:
    def test_deterministic(self):
        s = _spec()
        assert _spec_hash(s) == _spec_hash(s)

    def test_different_spec_different_hash(self):
        s1 = _spec("agent_a")
        s2 = _spec("agent_b")
        assert _spec_hash(s1) != _spec_hash(s2)

    def test_different_params_different_hash(self):
        s1 = _spec()
        s2 = _spec()
        s2 = s2._replace(stop_loss_pct=0.05) if hasattr(s2, '_replace') else s2
        import dataclasses
        s2 = dataclasses.replace(s1, stop_loss_pct=0.05)
        assert _spec_hash(s1) != _spec_hash(s2)


class TestRecordTrial:
    def test_records_trial(self, conn):
        from store.db import insert_agent
        insert_agent(conn, "trial_agent", "trial_agent", "2026-01-01T00:00:00Z", "{}")
        spec = _spec()
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 7, 1, tzinfo=timezone.utc)
        record_trial(conn, spec, start, end, deflated_sharpe=1.2, outcome="pass")
        row = conn.execute("SELECT * FROM backtest_trials").fetchone()
        assert row is not None
        assert row["agent_id"] == "trial_agent"
        assert row["deflated_sharpe"] == 1.2
        assert row["outcome"] == "pass"
        assert row["spec_hash"] is not None
        assert len(row["spec_hash"]) == 16

    def test_records_multiple_trials(self, conn):
        from store.db import insert_agent
        insert_agent(conn, "trial_agent", "trial_agent", "2026-01-01T00:00:00Z", "{}")
        spec = _spec()
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 7, 1, tzinfo=timezone.utc)
        record_trial(conn, spec, start, end, 1.0, "pass")
        record_trial(conn, spec, start, end, -0.5, "fail")
        count = conn.execute("SELECT COUNT(*) FROM backtest_trials").fetchone()[0]
        assert count == 2

    def test_trial_with_none_outcome(self, conn):
        from store.db import insert_agent
        insert_agent(conn, "trial_agent", "trial_agent", "2026-01-01T00:00:00Z", "{}")
        spec = _spec()
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 7, 1, tzinfo=timezone.utc)
        record_trial(conn, spec, start, end, 0.0, None)
        row = conn.execute("SELECT * FROM backtest_trials").fetchone()
        assert row["outcome"] is None

    def test_trial_count_on_overview(self, conn):
        from store.db import insert_agent
        from fastapi.testclient import TestClient
        from web.app import app
        insert_agent(conn, "trial_agent", "trial_agent", "2026-01-01T00:00:00Z", "{}")
        spec = _spec()
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 7, 1, tzinfo=timezone.utc)
        record_trial(conn, spec, start, end, 1.5, "pass")
        record_trial(conn, spec, start, end, -0.3, "fail")
        app.state.conn = conn
        app.state.provider = None
        app.state.config = {}
        r = TestClient(app).get("/")
        assert r.status_code == 200
        assert "Backtest Trials" in r.text
        assert "2" in r.text

    def test_trial_count_in_api(self, conn):
        from store.db import insert_agent
        from fastapi.testclient import TestClient
        from web.app import app
        insert_agent(conn, "trial_agent", "trial_agent", "2026-01-01T00:00:00Z", "{}")
        spec = _spec()
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 7, 1, tzinfo=timezone.utc)
        record_trial(conn, spec, start, end, 1.5, "pass")
        app.state.conn = conn
        app.state.provider = None
        app.state.config = {}
        r = TestClient(app).get("/api/desk-memory")
        assert r.status_code == 200
        assert r.json()["trial_count"] == 1
