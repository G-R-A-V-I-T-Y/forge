"""Tests for M10 executive action POST endpoints in web/app.py.

Covers all 8 exec endpoints plus the close-position endpoint:
  - /api/exec/trigger-reflection/{agent_id}
  - /api/exec/trigger-evaluation/{agent_id}
  - /api/exec/disable-entries/{agent_id}
  - /api/exec/enable-entries/{agent_id}
  - /api/exec/demote-agent/{agent_id}
  - /api/exec/promote-shadow/{agent_id}
  - /api/exec/go-live/{agent_id}
  - /api/exec/emergency-stop
  - /api/positions/{position_id}/close

Every exec action requires ``?reason=...`` and each writes an audit_log row.
"""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from store.db import (
    insert_agent,
    insert_account_snapshot,
    insert_trade,
    insert_position,
)
from market.heartbeat import write_heartbeat
from web.app import app

AGENT_ID = "jade_hawk"
NOW = "2026-07-09T12:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client(conn, config=None) -> TestClient:
    """Set app state on the module-level app and return a TestClient."""
    app.state.conn = conn
    app.state.provider = None
    app.state.config = config or {}
    # Clear llm_fn so the 503-returns test in TestTriggerReflection works
    # correctly. Tests that need llm_fn set it AFTER calling _client().
    if hasattr(app.state, "llm_fn"):
        del app.state.llm_fn
    return TestClient(app)


def _seed_agent(conn, agent_id=AGENT_ID, status="active") -> None:
    """Insert a minimal agent row + paper account snapshot."""
    insert_agent(conn, agent_id, agent_id, NOW, "{}")
    if status != "active":
        conn.execute("UPDATE agents SET status = ? WHERE id = ?", (status, agent_id))
        conn.commit()
    insert_account_snapshot(conn, agent_id, "paper", 50000.0, 50000.0)


def _count_audit_log(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def _assert_missing_reason(r):
    """Assert that a request without ?reason= returns 422."""
    assert r.status_code == 422
    detail = r.json().get("detail", [])
    assert any("reason" in str(d.get("loc", [])) for d in detail)


# ---------------------------------------------------------------------------
# Close position  (also tested in test_web_positions.py)
# ---------------------------------------------------------------------------

def _seed_open_position(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, NOW, "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    insert_trade(conn, {
        "id": "t1",
        "agent_id": AGENT_ID,
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
        "entry_timestamp": NOW,
        "status": "open",
    })
    insert_position(conn, {
        "id": "pos_t1",
        "agent_id": AGENT_ID,
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 145.20,
        "stop_loss_price": 143.00,
        "take_profit_price": 152.00,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "opened_at": NOW,
        "mode": "paper",
        "trade_id": "t1",
    })


def _heartbeat_config(tmp_path, price: float = 149.01) -> dict:
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {"SOL-PERP": {"price": price}},
        "cross_asset": {},
        "regime": {},
    })
    return {"desk": {"heartbeat_path": heartbeat_path, "heartbeat_interval_seconds": 300}}


def test_close_position_success(conn, tmp_path):
    _seed_open_position(conn)
    config = _heartbeat_config(tmp_path)
    r = _client(conn, config).post("/api/positions/pos_t1/close")
    assert r.status_code == 200
    data = r.json()
    assert data["trade_id"] == "t1"
    assert "exit_price" in data
    assert "pnl_pct" in data
    assert "pnl_usd" in data

    trade = conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone()
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "manual_close"

    position = conn.execute("SELECT * FROM positions WHERE id = ?", ("pos_t1",)).fetchone()
    assert position is None


def test_close_position_not_found(conn):
    _seed_open_position(conn)
    r = _client(conn).post("/api/positions/does_not_exist/close")
    assert r.status_code == 404
    assert "error" in r.json()


# ---------------------------------------------------------------------------
# Trigger reflection
# ---------------------------------------------------------------------------

class TestTriggerReflection:

    def test_success(self, conn):
        _seed_agent(conn)
        client = _client(conn)
        app.state.llm_fn = MagicMock()
        with patch("web.app.run_reflection") as mock_run:
            mock_run.return_value = None
            r = client.post(f"/api/exec/trigger-reflection/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "detail" in data
        mock_run.assert_called_once()

        # Audit-log verification
        assert _count_audit_log(conn) == 1
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["action"] == "trigger_reflection"
        assert row["agent_id"] == AGENT_ID
        assert row["reason"] == "test"

    def test_missing_reason(self, conn):
        _seed_agent(conn)
        app.state.llm_fn = MagicMock()
        r = _client(conn).post(f"/api/exec/trigger-reflection/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/trigger-reflection/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")

    def test_no_llm_fn_returns_503(self, conn):
        _seed_agent(conn)
        # app.state.llm_fn is not set (cleaned up by _client)
        r = _client(conn).post(f"/api/exec/trigger-reflection/{AGENT_ID}?reason=test")
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------

class TestTriggerEvaluation:

    def test_success(self, conn):
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/trigger-evaluation/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "decision" in data
        assert isinstance(data["decision"], dict)

        # Audit-log verification
        assert _count_audit_log(conn) == 1
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["action"] == "trigger_evaluation"
        assert row["agent_id"] == AGENT_ID
        assert row["reason"] == "test"

        # Verification: evaluations table has a row
        evals = conn.execute("SELECT * FROM evaluations").fetchall()
        assert len(evals) == 1
        assert evals[0]["agent_id"] == AGENT_ID

    def test_missing_reason(self, conn):
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/trigger-evaluation/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/trigger-evaluation/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Disable entries
# ---------------------------------------------------------------------------

class TestDisableEntries:

    def test_success(self, conn):
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/disable-entries/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True

        # Verify entry_disables row created
        rows = conn.execute("SELECT * FROM entry_disables").fetchall()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == AGENT_ID
        assert rows[0]["disabled_by"] == "human"
        assert rows[0]["reason"] == "test"
        assert rows[0]["enabled_at"] is None  # still disabled

        # Audit-log verification
        assert _count_audit_log(conn) == 1
        assert conn.execute("SELECT action FROM audit_log").fetchone()["action"] == "disable_entries"

    def test_missing_reason(self, conn):
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/disable-entries/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/disable-entries/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Enable entries
# ---------------------------------------------------------------------------

class TestEnableEntries:

    def test_success(self, conn):
        _seed_agent(conn)
        # First disable
        _client(conn).post(f"/api/exec/disable-entries/{AGENT_ID}?reason=disable_for_test")

        # Then enable
        r = _client(conn).post(f"/api/exec/enable-entries/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True

        # Verify enabled_at was set
        rows = conn.execute("SELECT * FROM entry_disables").fetchall()
        assert len(rows) == 1
        assert rows[0]["enabled_at"] is not None

        # Audit-log verification
        rows = conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()
        actions = [r["action"] for r in rows]
        assert actions == ["disable_entries", "enable_entries"]

    def test_success_when_no_active_disable(self, conn):
        """Enable with no existing disable should be a no-op (no rows to update)."""
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/enable-entries/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # No entry_disables row
        assert conn.execute("SELECT COUNT(*) FROM entry_disables").fetchone()[0] == 0

    def test_missing_reason(self, conn):
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/enable-entries/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/enable-entries/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Demote agent
# ---------------------------------------------------------------------------

class TestDemoteAgent:

    def test_success(self, conn):
        _seed_agent(conn, status="active")
        r = _client(conn).post(f"/api/exec/demote-agent/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "suspended"

        # Verify agent status changed
        row = conn.execute("SELECT status FROM agents WHERE id = ?", (AGENT_ID,)).fetchone()
        assert row["status"] == "suspended"

        # Audit-log verification
        assert _count_audit_log(conn) == 1
        assert conn.execute("SELECT action FROM audit_log").fetchone()["action"] == "demote_agent"

    def test_missing_reason(self, conn):
        _seed_agent(conn)
        r = _client(conn).post(f"/api/exec/demote-agent/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/demote-agent/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Promote to shadow
# ---------------------------------------------------------------------------

class TestPromoteShadow:

    def test_success_active(self, conn):
        _seed_agent(conn, status="active")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "shadow"

        row = conn.execute("SELECT status FROM agents WHERE id = ?", (AGENT_ID,)).fetchone()
        assert row["status"] == "shadow"

        assert _count_audit_log(conn) == 1
        assert conn.execute("SELECT action FROM audit_log").fetchone()["action"] == "promote_shadow"

    def test_success_rookie(self, conn):
        _seed_agent(conn, status="rookie")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        assert r.json()["status"] == "shadow"

    def test_success_suspended(self, conn):
        _seed_agent(conn, status="suspended")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        assert r.json()["status"] == "shadow"

    def test_ineligible_terminated(self, conn):
        _seed_agent(conn, status="terminated")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}?reason=test")
        assert r.status_code == 400
        assert "not eligible" in r.json().get("error", "").lower()

    def test_ineligible_shadow(self, conn):
        """Already shadow should return error."""
        _seed_agent(conn, status="shadow")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}?reason=test")
        assert r.status_code == 400
        assert "not eligible" in r.json().get("error", "").lower()

    def test_ineligible_live(self, conn):
        _seed_agent(conn, status="live")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}?reason=test")
        assert r.status_code == 400

    def test_missing_reason(self, conn):
        _seed_agent(conn, status="active")
        r = _client(conn).post(f"/api/exec/promote-shadow/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/promote-shadow/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Go live
# ---------------------------------------------------------------------------

class TestGoLive:

    def test_success(self, conn):
        _seed_agent(conn, status="shadow")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "live"

        row = conn.execute("SELECT status FROM agents WHERE id = ?", (AGENT_ID,)).fetchone()
        assert row["status"] == "live"

        assert _count_audit_log(conn) == 1
        assert conn.execute("SELECT action FROM audit_log").fetchone()["action"] == "go_live"

    def test_ineligible_not_shadow(self, conn):
        _seed_agent(conn, status="active")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}?reason=test")
        assert r.status_code == 400
        assert "shadow" in r.json().get("error", "").lower()

    def test_ineligible_rookie(self, conn):
        _seed_agent(conn, status="rookie")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}?reason=test")
        assert r.status_code == 400

    def test_ineligible_terminated(self, conn):
        _seed_agent(conn, status="terminated")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}?reason=test")
        assert r.status_code == 400

    def test_ineligible_suspended(self, conn):
        _seed_agent(conn, status="suspended")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}?reason=test")
        assert r.status_code == 400

    def test_ineligible_live(self, conn):
        _seed_agent(conn, status="live")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}?reason=test")
        assert r.status_code == 400

    def test_missing_reason(self, conn):
        _seed_agent(conn, status="shadow")
        r = _client(conn).post(f"/api/exec/go-live/{AGENT_ID}")
        _assert_missing_reason(r)

    def test_agent_not_found(self, conn):
        r = _client(conn).post("/api/exec/go-live/ghost?reason=test")
        assert r.status_code == 404
        assert "Agent not found" in r.json().get("error", "")


# ---------------------------------------------------------------------------
# Emergency stop
# ---------------------------------------------------------------------------

class TestEmergencyStop:

    def test_success(self, conn):
        _seed_agent(conn, status="active", agent_id="alpha")
        _seed_agent(conn, status="rookie", agent_id="beta")
        _seed_agent(conn, status="shadow", agent_id="gamma")
        _seed_agent(conn, status="suspended", agent_id="delta")  # already stopped

        r = _client(conn).post("/api/exec/emergency-stop?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        # 3 non-suspended agents should be affected
        assert data["agents_affected"] == 3

        # Verify all agents now suspended
        for aid in ("alpha", "beta", "gamma", "delta"):
            row = conn.execute("SELECT status FROM agents WHERE id = ?", (aid,)).fetchone()
            assert row["status"] == "suspended", f"{aid} should be suspended"

        # Audit-log verification
        assert _count_audit_log(conn) == 1
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["action"] == "emergency_stop"
        assert row["agent_id"] is None  # emergency_stop has no single agent_id
        assert row["reason"] == "test"

    def test_no_active_agents(self, conn):
        """When all agents are already suspended, agents_affected should be 0."""
        _seed_agent(conn, status="suspended")
        r = _client(conn).post("/api/exec/emergency-stop?reason=test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["agents_affected"] == 0

    def test_missing_reason(self, conn):
        _seed_agent(conn)
        r = _client(conn).post("/api/exec/emergency-stop")
        _assert_missing_reason(r)


# ---------------------------------------------------------------------------
# Audit-log cross-cutting verification
# ---------------------------------------------------------------------------

class TestAuditLog:

    def test_multiple_actions_all_logged(self, conn):
        """Call several different exec actions and verify all create audit rows."""
        _seed_agent(conn, status="active")
        c = _client(conn)
        app.state.llm_fn = MagicMock()

        with patch("web.app.run_reflection"):
            c.post(f"/api/exec/trigger-reflection/{AGENT_ID}?reason=reflect_now")
        c.post(f"/api/exec/trigger-evaluation/{AGENT_ID}?reason=eval_now")
        c.post(f"/api/exec/disable-entries/{AGENT_ID}?reason=stop_entries")
        c.post(f"/api/exec/demote-agent/{AGENT_ID}?reason=demote_now")
        c.post("/api/exec/emergency-stop?reason=panic")

        rows = conn.execute(
            "SELECT action, reason FROM audit_log ORDER BY id"
        ).fetchall()
        assert len(rows) == 5
        expected = [
            ("trigger_reflection", "reflect_now"),
            ("trigger_evaluation", "eval_now"),
            ("disable_entries", "stop_entries"),
            ("demote_agent", "demote_now"),
            ("emergency_stop", "panic"),
        ]
        for i, (action, reason) in enumerate(expected):
            assert rows[i]["action"] == action, f"Row {i} action mismatch"
            assert rows[i]["reason"] == reason, f"Row {i} reason mismatch"

    def test_reason_stored_in_audit_log(self, conn):
        """Verify the reason parameter persists correctly in audience_log."""
        _seed_agent(conn)
        custom_reason = "manual_intervention_q3"
        r = _client(conn).post(
            f"/api/exec/demote-agent/{AGENT_ID}?reason={custom_reason}"
        )
        assert r.status_code == 200

        row = conn.execute("SELECT reason FROM audit_log").fetchone()
        assert row["reason"] == custom_reason
