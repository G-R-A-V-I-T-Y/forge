"""Test multi-agent desk: competing positions, desk-wide registry, spawner."""

import json
from datetime import datetime, timezone

import pytest

from store.db import insert_agent, insert_account_snapshot, insert_position, insert_trade
from store.positions import get_all_open_positions, get_desk_positions_summary
from risk.gate import validate_order, RiskViolation
from meta.spawner import generate_agent_name, spawn_agent, check_against_graveyard


CONFIG = {
    "max_leverage": 10,
    "max_position_size_pct": 0.20,
    "max_concurrent_positions": 3,
}
BALANCE = 50000.0

VALID_ORDER = {
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_agent(conn, name: str, balance: float = 50000.0):
    """Helper: insert an agent and its account snapshot."""
    ts = _now()
    insert_agent(conn, name, name, ts, "{}")
    insert_account_snapshot(conn, name, "paper", balance, balance)


def open_position(conn, agent_id: str, asset: str, direction: str, price: float):
    """Helper: insert a trade and position row."""
    ts = _now()
    tid = f"{agent_id}_{asset}_{ts}"
    insert_trade(conn, {
        "id": tid, "agent_id": agent_id, "asset": asset,
        "direction": direction, "entry_price": price,
        "stop_loss_price": price * 0.98, "take_profit_price": price * 1.05,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "entry_timestamp": ts, "status": "open", "thesis_version": 1,
        "mode": "paper",
    })
    insert_position(conn, {
        "id": f"pos_{tid}", "agent_id": agent_id, "asset": asset,
        "direction": direction, "entry_price": price,
        "stop_loss_price": price * 0.98, "take_profit_price": price * 1.05,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "opened_at": ts, "mode": "paper", "trade_id": tid,
    })


class TestCompetingPositions:
    """Competing positions in the same asset are allowed — no blocking."""

    def test_two_agents_same_asset_both_pass_risk_gate(self):
        """Agent A and Agent B both enter SOL long — risk gate passes for
        the second agent because competing-position blocking does not exist."""
        # Both agents have 0 open positions each
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=0)
        # The gate only checks per-agent position count, not desk-wide
        # This is the critical test: there is NO competing-position check

    def test_two_agents_opposing_directions_both_pass(self):
        """Agent A long SOL, Agent B short SOL — both pass risk gate."""
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=0)
        short_order = {**VALID_ORDER, "direction": "short", "entry_price": 150.00, "stop_loss_price": 152.00}
        validate_order(short_order, BALANCE, CONFIG, open_position_count=0)

    def test_two_agents_same_asset_desk_positions(self, conn):
        """Both positions visible in get_all_open_positions()."""
        seed_agent(conn, "iron_moth")
        seed_agent(conn, "silver_basin")

        open_position(conn, "iron_moth", "SOL-PERP", "long", 145.00)
        open_position(conn, "silver_basin", "SOL-PERP", "short", 146.00)

        all_pos = get_all_open_positions(conn)
        assert len(all_pos) == 2

        agents_found = {p["agent_id"] for p in all_pos}
        assert "iron_moth" in agents_found
        assert "silver_basin" in agents_found

    def test_desk_positions_summary_contains_both(self, conn):
        """get_desk_positions_summary includes both agents' positions."""
        seed_agent(conn, "iron_moth")
        seed_agent(conn, "silver_basin")

        open_position(conn, "iron_moth", "SOL-PERP", "long", 145.00)
        open_position(conn, "silver_basin", "SOL-PERP", "short", 146.00)

        summary = get_desk_positions_summary(conn)
        assert "iron_moth" in summary
        assert "silver_basin" in summary
        assert "SOL" in summary

    def test_desk_positions_excludes_self(self, conn):
        """get_desk_positions_summary(exclude_agent_id=) omits that agent."""
        seed_agent(conn, "iron_moth")
        seed_agent(conn, "silver_basin")

        open_position(conn, "iron_moth", "SOL-PERP", "long", 145.00)
        open_position(conn, "silver_basin", "SOL-PERP", "short", 146.00)

        summary = get_desk_positions_summary(conn, exclude_agent_id="iron_moth")
        assert "iron_moth" not in summary
        assert "silver_basin" in summary

    def test_three_agents_one_asset_three_positions(self, conn):
        """Three agents all in BTC — all recorded in the desk registry."""
        for name in ["iron_moth", "silver_basin", "copper_vane"]:
            seed_agent(conn, name)
            open_position(conn, name, "BTC-PERP", "long", 65000.0)

        all_pos = get_all_open_positions(conn)
        assert len(all_pos) == 3


class TestSpawner:
    def test_generate_agent_name(self, conn):
        name = generate_agent_name(conn)
        assert "_" in name
        assert len(name) > 3

    def test_spawn_agent(self, conn):
        thesis = "# Test thesis"
        agent = spawn_agent(conn, "test_trader", thesis, starting_balance=50000.0)
        assert agent["name"] == "test_trader"
        assert agent["status"] == "rookie"

        # Account snapshot should exist
        row = conn.execute(
            "SELECT * FROM accounts WHERE agent_id = ?", ("test_trader",)
        ).fetchone()
        assert row is not None
        assert row["balance"] == 50000.0

    def test_spawn_agent_with_config(self, conn):
        overrides = {"wake_interval": 90}
        agent = spawn_agent(
            conn, "config_test", "# Config test",
            config_overrides=overrides, starting_balance=50000.0,
        )
        assert agent["name"] == "config_test"
        row = conn.execute(
            "SELECT config_json FROM agents WHERE id = ?", ("config_test",)
        ).fetchone()
        parsed = json.loads(row[0])
        assert parsed["wake_interval"] == 90

    def test_spawn_agent_duplicate_name(self, conn):
        spawn_agent(conn, "dupe_agent", "# First")
        # Second spawn with same name should be IGNORED (INSERT OR IGNORE)
        spawn_agent(conn, "dupe_agent", "# Second")
        count = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE id = ?", ("dupe_agent",)
        ).fetchone()[0]
        assert count == 1

    def test_check_against_graveyard(self, conn):
        """Stub: always returns True (unique)."""
        assert check_against_graveyard(conn, "any thesis") is True

    def test_generate_agent_name_does_not_return_reserved(self, conn):
        seed_agent(conn, "iron_moth")
        seed_agent(conn, "jade_hawk")
        name = generate_agent_name(conn)
        assert name not in ("iron_moth", "jade_hawk")


class TestRiskGateNoCompetingCheck:
    """Confirm risk/gate.py has no competing-position blocking."""

    def test_risk_gate_only_checks_per_agent_count(self):
        """The gate's open_position_count parameter is per-agent, not desk-wide.
        This test documents that behavior: two agents could each have 3 open
        positions in the same asset and pass if each is below the agent cap."""
        # Agent A has 2 positions, Agent B has 2 — both can add a third
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=2)
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=2)

    def test_agent_at_cap_blocked_other_agent_unaffected(self):
        """Agent A at max (3) is blocked; Agent B with 0 is not."""
        with pytest.raises(RiskViolation) as exc:
            validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=3)
        assert "concurrent positions" in exc.value.reason.lower()

        # Agent B still passes
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=0)
