"""Tests for meta/risk_officer.py — M9 criterion 7.

Covers the pieces added on top of the pre-existing kill-switch /
concentration / entry-gate machinery:
  - gross-exposure throttle (criterion b)
  - event-calendar blackout (criterion c)
  - the reduce-only action validator (the hard requirement)

Plus light coverage of the regime memo (a) and the per-agent kill flag (d).
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from market.heartbeat import write_heartbeat
from store.db import init_schema, insert_agent, insert_account_snapshot, insert_trade, insert_position
from meta.risk_officer import (
    RiskOfficer,
    RiskActionRejected,
    RiskOfficerOutput,
    validate_risk_actions,
    validate_risk_officer_output,
    run_risk_officer_job,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    init_schema(c)
    yield c
    c.close()


def _seed_agent_with_position(conn, agent_id: str, balance: float, notional: float | None = None):
    insert_agent(conn, agent_id, agent_id, "2026-06-01T00:00:00Z", "{}")
    insert_account_snapshot(conn, agent_id, "paper", balance, balance)
    if notional is not None:
        trade_id = f"trade_{agent_id}"
        insert_trade(conn, {
            "id": trade_id,
            "agent_id": agent_id,
            "mode": "paper",
            "asset": "BTC-PERP",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss_price": 95.0,
            "take_profit_price": 110.0,
            "leverage": 1,
            "position_size_pct": 0.1,
            "notional_usd": notional,
            "entry_timestamp": "2026-07-10T00:00:00Z",
            "status": "open",
        })
        insert_position(conn, {
            "id": f"pos_{agent_id}",
            "agent_id": agent_id,
            "asset": "BTC-PERP",
            "direction": "long",
            "entry_price": 100.0,
            "stop_loss_price": 95.0,
            "take_profit_price": 110.0,
            "leverage": 1,
            "position_size_pct": 0.1,
            "notional_usd": notional,
            "opened_at": "2026-07-10T00:00:00Z",
            "mode": "paper",
            "trade_id": trade_id,
        })


def _config(**desk_overrides):
    desk = {
        "max_gross_exposure_mult": 2.0,
        "event_calendar": [],
        "max_position_size": 10_000_000,  # neutralize the unrelated per-agent size check
    }
    desk.update(desk_overrides)
    return {"desk": desk, "max_position_size": 10_000_000}


# ---------------------------------------------------------------------------
# Criterion (b): gross-exposure throttle
# ---------------------------------------------------------------------------

def test_gross_exposure_throttle(conn):
    # Total desk equity = 1000 + 1000 + 500 = 2500; threshold = 2x = 5000.
    _seed_agent_with_position(conn, "agent_a", balance=1000, notional=15000)
    _seed_agent_with_position(conn, "agent_b", balance=1000, notional=10000)
    _seed_agent_with_position(conn, "agent_c", balance=500, notional=2000)
    # Aggregate gross notional = 27000, far over the 5000 threshold.

    officer = RiskOfficer(conn, _config())
    throttled = officer.gross_exposure_throttle()

    # Highest-exposure agents disabled first, only as many as needed:
    # remove agent_a (27000 -> 12000, still > 5000), then agent_b
    # (12000 -> 2000, now <= 5000) -> stop. agent_c never touched.
    assert throttled == ["agent_a", "agent_b"]

    for agent_id in throttled:
        officer.disable_entry(agent_id, "gross exposure throttle test")

    disabled_ids = {
        row["agent_id"] for row in conn.execute(
            "SELECT agent_id FROM entry_disables WHERE enabled_at IS NULL"
        ).fetchall()
    }
    assert disabled_ids == {"agent_a", "agent_b"}


def test_gross_exposure_throttle_no_action_under_threshold(conn):
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=5000)
    officer = RiskOfficer(conn, _config())
    assert officer.gross_exposure_throttle() == []


def test_throttle_idempotent_across_cycles(conn):
    """Repeated run_cycle calls while over-exposed must not stack duplicate
    entry_disables rows — one open row per throttled agent, total."""
    _seed_agent_with_position(conn, "agent_a", balance=1000, notional=15000)
    _seed_agent_with_position(conn, "agent_b", balance=1000, notional=500)
    officer = RiskOfficer(conn, _config())

    officer.run_cycle()
    officer.run_cycle()
    officer.run_cycle()

    rows = conn.execute(
        """SELECT agent_id, COUNT(*) AS n FROM entry_disables
           WHERE enabled_at IS NULL GROUP BY agent_id"""
    ).fetchall()
    counts = {r["agent_id"]: r["n"] for r in rows}
    assert counts == {"agent_a": 1}


def test_throttle_reenables_when_exposure_falls(conn):
    """Once desk exposure falls back under the threshold, the officer must
    restore the entry gates it closed (restore-to-baseline, not add-risk:
    it only ever lifts its own disables)."""
    _seed_agent_with_position(conn, "agent_a", balance=1000, notional=15000)
    _seed_agent_with_position(conn, "agent_b", balance=1000, notional=500)
    officer = RiskOfficer(conn, _config())

    report = officer.run_cycle()
    assert report["gross_exposure_throttled_agents"] == ["agent_a"]
    assert officer.is_entry_gate_open("agent_a") is False

    # Position closes; exposure back under 2x equity.
    conn.execute("DELETE FROM positions WHERE agent_id = 'agent_a'")
    conn.commit()

    report = officer.run_cycle()
    assert report["gross_exposure_throttled_agents"] == []
    assert officer.is_entry_gate_open("agent_a") is True

    # A HUMAN-set disable must survive the officer's reconciliation.
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        "VALUES ('agent_b', 'human', '2026-07-10T00:00:00Z', 'human stop')"
    )
    conn.commit()
    officer.run_cycle()
    human_row = conn.execute(
        "SELECT 1 FROM entry_disables WHERE agent_id = 'agent_b' AND enabled_at IS NULL"
    ).fetchone()
    assert human_row is not None


def test_transient_gate_closures_not_materialized(conn):
    """A desk-wide transient condition (event blackout) must close the live
    gate WITHOUT writing entry_disables rows — otherwise a 2h blackout
    would outlive itself and freeze the desk permanently.

    The event is 1h from now so run_cycle really executes INSIDE the
    blackout window (run_cycle reads the real clock)."""
    from datetime import timedelta

    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    event_at = datetime.now(timezone.utc) + timedelta(hours=1)
    config = _config(event_calendar=[
        {"name": "FOMC", "at": event_at.isoformat().replace("+00:00", "Z")}
    ])
    officer = RiskOfficer(conn, config)

    # We really are inside the window right now.
    assert officer.event_blackout_active() is not None
    assert officer.is_entry_gate_open("agent_a") is False

    report = officer.run_cycle()
    assert report["event_blackout"] is not None
    assert report["agents"]["agent_a"]["entry_gate_open"] is False

    rows = conn.execute(
        "SELECT COUNT(*) FROM entry_disables WHERE enabled_at IS NULL"
    ).fetchone()[0]
    assert rows == 0, "blackout/kill-switch closures must stay dynamic, never persisted"

    after_event = event_at + timedelta(minutes=30)
    assert officer.is_entry_gate_open("agent_a", now=after_event) is True


def test_run_cycle_requires_desk_config(conn):
    """Per the repo config convention, a truthy config missing the desk
    block must fail loudly — the throttle/blackout/memo protections must
    never be skipped silently."""
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    officer = RiskOfficer(conn, {"not_desk": {}})
    with pytest.raises(KeyError):
        officer.run_cycle()


def test_gross_exposure_throttle_requires_desk_config(conn):
    officer = RiskOfficer(conn, {"not_desk": {}})
    with pytest.raises(KeyError):
        officer.gross_exposure_throttle()


# ---------------------------------------------------------------------------
# Criterion (c): event-calendar blackout
# ---------------------------------------------------------------------------

def test_event_blackout_blocks_entries(conn):
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    config = _config(event_calendar=[{"name": "FOMC", "at": "2026-07-29T18:00:00Z"}])
    officer = RiskOfficer(conn, config)

    before_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    within_window = datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc)  # 1.5h before
    assert officer.event_blackout_active(within_window) is not None
    assert officer.is_entry_gate_open("agent_a", now=within_window) is False

    outside_window = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)  # 3h before
    assert officer.event_blackout_active(outside_window) is None
    assert officer.is_entry_gate_open("agent_a", now=outside_window) is True

    after_event = datetime(2026, 7, 29, 18, 30, tzinfo=timezone.utc)  # 30min after
    assert officer.event_blackout_active(after_event) is None
    assert officer.is_entry_gate_open("agent_a", now=after_event) is True, (
        "blackout must clear automatically once the event passes"
    )

    # Existing positions are never touched by the blackout gate.
    after_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert after_positions == before_positions


def test_event_blackout_empty_calendar_is_valid(conn):
    officer = RiskOfficer(conn, _config(event_calendar=[]))
    assert officer.event_blackout_active() is None


def test_event_blackout_malformed_entry_fails_loudly(conn):
    officer = RiskOfficer(conn, _config(event_calendar=[{"name": "FOMC"}]))  # missing "at"
    with pytest.raises(ValueError):
        officer.event_blackout_active()

    officer2 = RiskOfficer(conn, _config(event_calendar=[{"name": "FOMC", "at": "not-a-date"}]))
    with pytest.raises(ValueError):
        officer2.event_blackout_active()


# ---------------------------------------------------------------------------
# The hard requirement: reduce-only validator
# ---------------------------------------------------------------------------

def test_risk_officer_cannot_add_risk(conn):
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)

    # 1. Unknown action type -- routing a new entry is never allowed.
    with pytest.raises(RiskActionRejected):
        validate_risk_actions(
            [{"type": "open_position", "agent_id": "agent_a", "size_pct": 0.5}], conn
        )

    # 2. A "disable_entry" that smuggles a stop-loss override -- widening/
    #    touching a stop is never allowed, regardless of the action type.
    with pytest.raises(RiskActionRejected):
        validate_risk_actions(
            [{"type": "disable_entry", "agent_id": "agent_a", "reason": "x",
              "stop_loss_price": 999}], conn
        )

    # 3. A "disable_entry" that smuggles a size increase.
    with pytest.raises(RiskActionRejected):
        validate_risk_actions(
            [{"type": "disable_entry", "agent_id": "agent_a", "reason": "x",
              "position_size_pct": 0.9}], conn
        )

    # 4. enable_entry for an agent the officer never disabled -- restoring
    #    only the officer's own throttle is allowed, nothing else.
    with pytest.raises(RiskActionRejected):
        validate_risk_actions([{"type": "enable_entry", "agent_id": "agent_a"}], conn)

    # 5. enable_entry cannot lift a human-set disable.
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        "VALUES ('agent_a', 'human', '2026-07-10T00:00:00Z', 'human stop')"
    )
    conn.commit()
    with pytest.raises(RiskActionRejected):
        validate_risk_actions([{"type": "enable_entry", "agent_id": "agent_a"}], conn)

    # 6. End-to-end: a hypothetical buggy rule emits a disable_entry action
    #    that ALSO tries to bump position size. The apply path must refuse
    #    it and leave the database untouched.
    officer = RiskOfficer(conn, _config())
    before_disables = conn.execute("SELECT COUNT(*) FROM entry_disables").fetchone()[0]
    before_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    def _buggy_rule_output():
        return [{
            "type": "disable_entry", "agent_id": "agent_a", "reason": "throttle",
            "position_size_pct": 0.9,  # bug: should never be here
        }]

    with pytest.raises(RiskActionRejected):
        officer.apply_actions(_buggy_rule_output())

    after_disables = conn.execute("SELECT COUNT(*) FROM entry_disables").fetchone()[0]
    after_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert after_disables == before_disables
    assert after_positions == before_positions


def test_disable_entry_and_enable_entry_flow_through_validator(conn):
    """Sanity check: the pre-existing kill-switch/concentration apply path
    (disable_entry/enable_entry) still works end-to-end once routed
    through validate_risk_actions -- the officer can always restore its
    own throttle."""
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    officer = RiskOfficer(conn, _config())

    officer.disable_entry("agent_a", "test disable")
    row = conn.execute(
        "SELECT disabled_by FROM entry_disables WHERE agent_id = 'agent_a' AND enabled_at IS NULL"
    ).fetchone()
    assert row["disabled_by"] == "risk_officer"

    officer.enable_entry("agent_a")
    remaining = conn.execute(
        "SELECT 1 FROM entry_disables WHERE agent_id = 'agent_a' AND enabled_at IS NULL"
    ).fetchone()
    assert remaining is None


# ---------------------------------------------------------------------------
# Criterion (a): regime memo
# ---------------------------------------------------------------------------

def test_regime_memo_built_and_persisted(conn, tmp_path):
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {},
        "cross_asset": {},
        "regime": {
            "regime_tag": "trending_bull",
            "average_volatility": 0.42,
            "average_funding": 0.0001,
            "risk_on_score": 0.7,
            "trend_score": 1.2,
            "crypto_fear_index": 55,
            "btc_dominance": 0.6,
        },
        "events": {},
    })
    config = _config()
    config["desk"]["heartbeat_path"] = heartbeat_path
    config["desk"]["heartbeat_interval_seconds"] = 300

    officer = RiskOfficer(conn, config)
    memo = officer.build_regime_memo()
    assert memo is not None
    assert memo["regime_tag"] == "trending_bull"
    assert memo["average_volatility"] == 0.42

    officer.persist_regime_memo(memo)
    stored = RiskOfficer.latest_regime_memo(conn)
    assert stored["regime_tag"] == "trending_bull"


def test_regime_memo_none_without_heartbeat(conn, tmp_path):
    config = _config()
    config["desk"]["heartbeat_path"] = str(tmp_path / "does_not_exist.json")
    officer = RiskOfficer(conn, config)
    assert officer.build_regime_memo() is None


# ---------------------------------------------------------------------------
# Criterion (d): per-agent kill flag
# ---------------------------------------------------------------------------

def test_agent_is_killed_reflects_status(conn):
    insert_agent(conn, "agent_a", "agent_a", "2026-06-01T00:00:00Z", "{}")
    insert_agent(conn, "agent_b", "agent_b", "2026-06-01T00:00:00Z", "{}")
    conn.execute("UPDATE agents SET status = 'suspended' WHERE id = 'agent_a'")
    conn.execute("UPDATE agents SET status = 'active' WHERE id = 'agent_b'")
    conn.commit()

    officer = RiskOfficer(conn, _config())
    assert officer.agent_is_killed("agent_a") is True
    assert officer.agent_is_killed("agent_b") is False


# ---------------------------------------------------------------------------
# RiskOfficerOutput frozen dataclass
# ---------------------------------------------------------------------------

def test_risk_officer_output_is_frozen():
    output = RiskOfficerOutput(entry_disabled_agents=["a"], reason="test", regime="crisis")
    assert output.entry_disabled_agents == ["a"]
    assert output.reason == "test"
    assert output.regime == "crisis"
    with pytest.raises(AttributeError):
        output.reason = "changed"


def test_risk_officer_output_defaults():
    output = RiskOfficerOutput()
    assert output.entry_disabled_agents == []
    assert output.reason == ""
    assert output.regime == ""


# ---------------------------------------------------------------------------
# run_risk_officer_job
# ---------------------------------------------------------------------------

def test_run_risk_officer_job_clear_desk(conn):
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    config = _config()
    output = run_risk_officer_job(conn, config)
    assert isinstance(output, RiskOfficerOutput)
    assert output.reason == "all clear"
    assert output.entry_disabled_agents == []


def test_run_risk_officer_job_throttle_disables(conn):
    _seed_agent_with_position(conn, "agent_a", balance=1000, notional=15000)
    _seed_agent_with_position(conn, "agent_b", balance=1000, notional=10000)
    output = run_risk_officer_job(conn, _config())
    assert "agent_a" in output.entry_disabled_agents
    assert "agent_b" in output.entry_disabled_agents
    assert "throttle" in output.reason.lower() or "exposure" in output.reason.lower()


def test_run_risk_officer_job_event_blackout_disables_all(conn):
    from datetime import timedelta

    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    _seed_agent_with_position(conn, "agent_b", balance=10000, notional=1000)
    event_at = datetime.now(timezone.utc) + timedelta(hours=1)
    config = _config(event_calendar=[
        {"name": "FOMC", "at": event_at.isoformat().replace("+00:00", "Z")}
    ])
    output = run_risk_officer_job(conn, config)
    assert "agent_a" in output.entry_disabled_agents
    assert "agent_b" in output.entry_disabled_agents
    assert "blackout" in output.reason.lower()


def test_run_risk_officer_job_surfaces_non_officer_disables(conn):
    """A human/other-originated entry_disables row (the exact shape that
    got stuck invisibly for 8 days, 2026-07-12..07-15) must show up in the
    output even though the officer did not create it and can never clear
    it -- entry_disabled_agents (the reduce-only-invariant field) must NOT
    include it, since a later human re-enable via the web endpoint bypasses
    validate_risk_officer_output and would look like an illegal risk
    decrease if this field claimed ownership of it."""
    _seed_agent_with_position(conn, "agent_a", balance=10000, notional=1000)
    conn.execute(
        "INSERT INTO entry_disables (agent_id, disabled_by, disabled_at, reason) "
        "VALUES ('agent_a', 'human', '2026-07-12T18:11:10Z', 'Entry blocked by risk check')"
    )
    conn.commit()
    output = run_risk_officer_job(conn, _config())

    assert "agent_a" not in output.entry_disabled_agents
    assert len(output.other_open_disables) == 1
    assert output.other_open_disables[0]["agent_id"] == "agent_a"
    assert output.other_open_disables[0]["disabled_by"] == "human"
    assert output.other_open_disables[0]["reason"] == "Entry blocked by risk check"
    assert "agent_a" in output.reason


def test_run_risk_officer_job_regime_populated(conn, tmp_path):
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {},
        "cross_asset": {},
        "regime": {"regime_tag": "trending_bull", "average_volatility": 0.42},
        "events": {},
    })
    config = _config()
    config["desk"]["heartbeat_path"] = heartbeat_path
    config["desk"]["heartbeat_interval_seconds"] = 300

    output = run_risk_officer_job(conn, config)
    assert output.regime == "trending_bull"


# ---------------------------------------------------------------------------
# validate_risk_officer_output
# ---------------------------------------------------------------------------

def test_validate_risk_officer_output_allows_same_state():
    prev = RiskOfficerOutput(entry_disabled_agents=["a", "b"], reason="x", regime="crisis")
    new = RiskOfficerOutput(entry_disabled_agents=["a", "b"], reason="y", regime="crisis")
    validate_risk_officer_output(prev, new)


def test_validate_risk_officer_output_allows_adding_disables():
    prev = RiskOfficerOutput(entry_disabled_agents=["a"], reason="x", regime="crisis")
    new = RiskOfficerOutput(entry_disabled_agents=["a", "b"], reason="y", regime="crisis")
    validate_risk_officer_output(prev, new)


def test_validate_risk_officer_output_allows_same_regime():
    prev = RiskOfficerOutput(entry_disabled_agents=[], reason="x", regime="range_low_vol")
    new = RiskOfficerOutput(entry_disabled_agents=[], reason="y", regime="range_low_vol")
    validate_risk_officer_output(prev, new)


def test_validate_risk_officer_output_catches_un_disable():
    from risk.gate import RiskViolation

    prev = RiskOfficerOutput(entry_disabled_agents=["a", "b"], reason="x", regime="crisis")
    new = RiskOfficerOutput(entry_disabled_agents=["a"], reason="y", regime="crisis")
    with pytest.raises(RiskViolation, match="reduce-only"):
        validate_risk_officer_output(prev, new)


def test_validate_risk_officer_output_catches_regime_loosening():
    from risk.gate import RiskViolation

    prev = RiskOfficerOutput(entry_disabled_agents=[], reason="x", regime="crisis")
    new = RiskOfficerOutput(entry_disabled_agents=[], reason="y", regime="trending_bull")
    with pytest.raises(RiskViolation, match="regime loosened"):
        validate_risk_officer_output(prev, new)


def test_validate_risk_officer_output_allows_stricter_regime():
    prev = RiskOfficerOutput(entry_disabled_agents=[], reason="x", regime="range_low_vol")
    new = RiskOfficerOutput(entry_disabled_agents=[], reason="y", regime="crisis")
    validate_risk_officer_output(prev, new)


def test_validate_risk_officer_output_allows_empty_to_disabled():
    prev = RiskOfficerOutput()
    new = RiskOfficerOutput(entry_disabled_agents=["a"], reason="kill switch", regime="crisis")
    validate_risk_officer_output(prev, new)


# ---------------------------------------------------------------------------
# risk/gate.py integration with risk_officer_output
# ---------------------------------------------------------------------------

def test_risk_gate_blocks_disabled_agent():
    from risk.gate import validate_order, RiskViolation

    VALID_ORDER = {
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 5,
        "position_size_pct": 0.10,
    }
    output = RiskOfficerOutput(entry_disabled_agents=["agent_a"], reason="throttle")
    with pytest.raises(RiskViolation, match="risk officer disabled"):
        validate_order(VALID_ORDER, 10000, {"max_leverage": 20, "max_position_size_pct": 0.25,
                      "max_concurrent_positions": 5}, open_position_count=0,
                      agent_id="agent_a", risk_officer_output=output)


def test_risk_gate_allows_non_disabled_agent():
    from risk.gate import validate_order

    VALID_ORDER = {
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 5,
        "position_size_pct": 0.10,
    }
    output = RiskOfficerOutput(entry_disabled_agents=["agent_b"], reason="throttle")
    validate_order(VALID_ORDER, 10000, {"max_leverage": 20, "max_position_size_pct": 0.25,
                  "max_concurrent_positions": 5}, open_position_count=0,
                  agent_id="agent_a", risk_officer_output=output)


def test_risk_gate_ignores_output_when_none():
    from risk.gate import validate_order

    VALID_ORDER = {
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 5,
        "position_size_pct": 0.10,
    }
    validate_order(VALID_ORDER, 10000, {"max_leverage": 20, "max_position_size_pct": 0.25,
                  "max_concurrent_positions": 5}, open_position_count=0,
                  agent_id="agent_a", risk_officer_output=None)


def test_risk_gate_backwards_compatible_without_new_params():
    from risk.gate import validate_order

    VALID_ORDER = {
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 5,
        "position_size_pct": 0.10,
    }
    validate_order(VALID_ORDER, 10000, {"max_leverage": 20, "max_position_size_pct": 0.25,
                  "max_concurrent_positions": 5}, open_position_count=0)
