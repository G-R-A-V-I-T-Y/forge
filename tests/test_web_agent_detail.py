"""Tests for the agent detail page's spec diff view and calibration report."""
import json

from fastapi.testclient import TestClient

from store.db import insert_agent, insert_trade
from web.app import app

AGENT_ID = "jade_hawk"


def _client(conn) -> TestClient:
    app.state.conn = conn
    return TestClient(app)


def _seed_agent(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")


def _insert_spec(conn, spec_version, yaml_text, status="active", thesis_version=1):
    conn.execute(
        """INSERT INTO specs
               (agent_id, spec_version, thesis_version, yaml_text,
                status, deployed_at, rejection_reason, validation_errors)
           VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
        (
            AGENT_ID,
            spec_version,
            thesis_version,
            yaml_text,
            status,
            f"2026-07-0{spec_version}T00:00:00Z",
        ),
    )
    conn.commit()


def _insert_closed_trade(conn, trade_id, confidence, result):
    pnl_pct = 0.03 if result == "win" else -0.02
    insert_trade(conn, {
        "id": trade_id,
        "agent_id": AGENT_ID,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 98.0,
        "take_profit_price": 105.0,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-06-29T14:37:12Z",
        "exit_price": 103.0 if result == "win" else 98.5,
        "exit_timestamp": "2026-06-29T18:00:00Z",
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_pct * 5000.0,
        "status": "closed",
        "confidence": confidence,
        "result": result,
    })


def test_agent_detail_no_spec_history_shows_graceful_state(conn):
    _seed_agent(conn)
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "No spec versions deployed yet" in r.text


def test_agent_detail_single_spec_version_no_diff(conn):
    _seed_agent(conn)
    _insert_spec(conn, 1, "agent_id: jade_hawk\nspec_version: 1\n")
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "v1" in r.text
    assert "No diff to show" in r.text


def test_agent_detail_two_spec_versions_renders_diff(conn):
    _seed_agent(conn)
    _insert_spec(conn, 1, "agent_id: jade_hawk\nspec_version: 1\nentry:\n  confidence_threshold: 0.5\n", status="inactive")
    _insert_spec(conn, 2, "agent_id: jade_hawk\nspec_version: 2\nentry:\n  confidence_threshold: 0.7\n", status="active")
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "spec_v1" in r.text
    assert "spec_v2" in r.text
    assert "confidence_threshold: 0.5" in r.text
    assert "confidence_threshold: 0.7" in r.text
    assert "No diff to show" not in r.text


def test_agent_detail_no_trades_calibration_graceful_state(conn):
    _seed_agent(conn)
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "Not enough closed trades" in r.text


def test_agent_detail_calibration_report_buckets_and_win_rate(conn):
    _seed_agent(conn)
    _insert_closed_trade(conn, "t1", 0.65, "win")
    _insert_closed_trade(conn, "t2", 0.68, "loss")
    _insert_closed_trade(conn, "t3", 0.85, "win")
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "0.6-0.7" in r.text
    assert "0.8-0.9" in r.text
    # bucket 0.6-0.7 has 2 trades, 1 win -> 50.0%
    assert "50.0%" in r.text
    # bucket 0.8-0.9 has 1 trade, 1 win -> 100.0%
    assert "100.0%" in r.text


def test_agent_detail_no_reflections_shows_graceful_state(conn):
    _seed_agent(conn)
    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "No reflection cycles recorded yet" in r.text


def test_agent_detail_reflection_cycle_shows_digest_diff_and_hypotheses(conn):
    """M10 'Done when': the agent page shows the dossier digest, hypothesis
    outcomes, and thesis/spec diffs for the cycle."""
    _seed_agent(conn)
    cur = conn.execute(
        """INSERT INTO reflections
               (agent_id, triggered_at, outcome, research_findings_json, proposed_changes)
           VALUES (?, ?, ?, ?, ?)""",
        (
            AGENT_ID, "2026-07-10T00:00:00Z", "deployed",
            json.dumps({"closed_trades": 25, "regimes": ["trending"]}),
            json.dumps({"spec_diff_summary": {"from_version": 1, "to_version": 2}}),
        ),
    )
    reflection_id = cur.lastrowid
    conn.execute(
        """INSERT INTO hypotheses
               (agent_id, reflection_id, claim, predicted_effect, status,
                effect_observed, created_at, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            AGENT_ID, reflection_id, "funding term improves entries",
            "regret decreases", "validated", 1.5,
            "2026-07-10T00:00:00Z", "2026-07-12T00:00:00Z",
        ),
    )
    conn.commit()

    r = _client(conn).get(f"/agents/{AGENT_ID}")
    assert r.status_code == 200
    assert "Reflection Cycles" in r.text
    assert "funding term improves entries" in r.text
    assert "VALIDATED" in r.text
    assert "closed_trades" in r.text  # dossier digest JSON rendered
    assert "spec_diff_summary" in r.text  # thesis/spec diff JSON rendered
