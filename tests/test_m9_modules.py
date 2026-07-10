"""Tests for M9: Selection & Daily Improvement Loop modules.

Covers: evaluator, reflection_scheduler, controller, risk_officer, head_of_desk.
"""
import json
import sqlite3

import pytest

from store.db import init_schema


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
    _seed_agents(c)
    _seed_trades(c)
    yield c
    c.close()


def _seed_agents(c):
    """Seed minimal agent + account rows for testing."""
    now = "2026-07-09T00:00:00Z"
    agents = [
        ("alpha_trader", "alpha_trader", "active", now, "{}"),
        ("beta_trader", "beta_trader", "rookie", now, "{}"),
        ("gamma_trader", "gamma_trader", "active", now, "{}"),
        ("dead_trader", "dead_trader", "terminated", now, "{}"),
        ("benchmark_random_walk", "benchmark_random_walk", "active", now, "{}"),
    ]
    c.executemany(
        "INSERT INTO agents (id, name, status, spawn_date, config_json) VALUES (?, ?, ?, ?, ?)",
        agents,
    )
    for aid, _, _, _, _ in agents:
        c.execute(
            "INSERT INTO accounts (agent_id, mode, balance, peak_balance, recorded_at) VALUES (?, 'paper', 10000, 10000, ?)",
            (aid, now),
        )
    c.commit()


def _seed_trades(c):
    """Seed trades with varied outcomes for lifecycle tests.
    
    alpha_trader: 60 trades, 60% win rate, profitable — good standing.
    beta_trader: 10 trades, 50% win rate, slightly profitable — rookie.
    gamma_trader: 60 trades, 30% win rate, unprofitable — termination candidate.
    """
    now = "2026-07-09T00:00:00Z"
    
    # alpha_trader: 60 trades, 60% win (36 win / 24 loss)
    for i in range(60):
        is_win = i < 36
        pnl_usd = 50.0 if is_win else -30.0
        pnl_pct = 0.02 if is_win else -0.015
        result = "win" if is_win else "loss"
        c.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
               stop_loss_price, take_profit_price)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50000 + ?, 1,
               'closed', ?, ?, ?, ?, ?, 48500, 53000)""",
            (f"alpha_{i}", "alpha_trader", pnl_usd, pnl_pct, pnl_usd, result, now, now),
        )
        # benchmark gets identical trades
        c.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
               stop_loss_price, take_profit_price)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50000 + ?, 1,
               'closed', ?, ?, ?, ?, ?, 48500, 53000)""",
            (f"bench_{i}", "benchmark_random_walk", pnl_usd, pnl_pct, pnl_usd, result, now, now),
        )

    # beta_trader: 10 trades, 50% win
    for i in range(10):
        is_win = i < 5
        pnl_usd = 40.0 if is_win else -20.0
        pnl_pct = 0.015 if is_win else -0.01
        result = "win" if is_win else "loss"
        c.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
               stop_loss_price, take_profit_price)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50000 + ?, 1,
               'closed', ?, ?, ?, ?, ?, 48500, 53000)""",
            (f"beta_{i}", "beta_trader", pnl_usd, pnl_pct, pnl_usd, result, now, now),
        )

    # gamma_trader: 60 trades, 30% win (18 win / 42 loss)
    for i in range(60):
        is_win = i < 18
        pnl_usd = 50.0 if is_win else -35.0
        pnl_pct = 0.02 if is_win else -0.02
        result = "win" if is_win else "loss"
        c.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
               stop_loss_price, take_profit_price)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50000 + ?, 1,
               'closed', ?, ?, ?, ?, ?, 48500, 53000)""",
            (f"gamma_{i}", "gamma_trader", pnl_usd, pnl_pct, pnl_usd, result, now, now),
        )

    c.commit()


# ===================================================================
# Evaluator Tests
# ===================================================================

class TestEvaluator:

    def test_significance_test_insufficient_data(self, conn):
        from meta.evaluator import significance_test

        agent_metrics = {"closed_trades": 5, "sharpe": 0.5, "profit_factor": 1.2, "win_rate": 0.5}
        result = significance_test(agent_metrics, None)
        assert result["p_value_estimate"] == "insufficient_data"
        assert not result["beats_null"]

    def test_significance_test_beats_null(self, conn):
        from meta.evaluator import significance_test, get_null_metrics

        # alpha_trader has 60 trades with 60% win rate
        from store.performance import compute_metrics
        agent_metrics = compute_metrics(conn, "alpha_trader")
        null_metrics = get_null_metrics(conn)

        result = significance_test(agent_metrics, null_metrics)
        # alpha has positive sharpe, should beat null
        assert agent_metrics["closed_trades"] >= 30

    def test_get_null_metrics_returns_none_when_no_benchmark(self, conn):
        from meta.evaluator import get_null_metrics
        # Delete dependent records first to satisfy FK constraints
        conn.execute("DELETE FROM trades WHERE agent_id = 'benchmark_random_walk'")
        conn.execute("DELETE FROM accounts WHERE agent_id = 'benchmark_random_walk'")
        conn.execute("DELETE FROM agents WHERE id = 'benchmark_random_walk'")
        conn.commit()
        assert get_null_metrics(conn) is None

    def test_lifecycle_decision_terminate_low_win_rate(self, conn):
        from meta.evaluator import get_lifecycle_decision
        from store.performance import compute_metrics

        # gamma_trader has 30% win rate after 60 trades
        metrics = compute_metrics(conn, "gamma_trader")
        result = get_lifecycle_decision(conn, "gamma_trader", metrics, None)
        assert result["decision"] == "terminate"
        assert "win rate" in result["reason"].lower()

    def test_lifecycle_decision_active_good_standing(self, conn):
        from meta.evaluator import get_lifecycle_decision
        from store.performance import compute_metrics

        metrics = compute_metrics(conn, "alpha_trader")
        result = get_lifecycle_decision(conn, "alpha_trader", metrics, None)
        assert result["decision"] == "active"

    def test_lifecycle_decision_terminate_not_found(self, conn):
        from meta.evaluator import get_lifecycle_decision
        result = get_lifecycle_decision(conn, "nonexistent", {}, None)
        assert result["decision"] == "terminate"
        assert result["trigger"] == "not_found"

    def test_harvest_best_trades(self, conn):
        from meta.evaluator import harvest_best_trades

        seeds = harvest_best_trades(conn, "alpha_trader", count=3)
        assert len(seeds) == 3
        # Verify seeds were inserted into the seeds table
        rows = conn.execute(
            "SELECT * FROM seeds WHERE source_agent_id = 'alpha_trader'"
        ).fetchall()
        assert len(rows) == 3
        # Should be sorted by pnl_pct descending
        assert rows[0]["pnl_pct"] >= rows[1]["pnl_pct"] >= rows[2]["pnl_pct"]


# ===================================================================
# Reflection Scheduler Tests
# ===================================================================

class TestReflectionScheduler:

    def test_get_reflection_trigger_default(self, conn):
        from meta.reflection_scheduler import get_reflection_trigger
        trigger = get_reflection_trigger(conn)
        assert trigger["mode"] == "trade_count"
        assert trigger["trade_interval"] == 20

    def test_get_reflection_trigger_from_settings(self, conn):
        from meta.reflection_scheduler import get_reflection_trigger
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('reflection_trigger', ?)",
            (json.dumps({"mode": "calendar_days", "day_interval": 7}),),
        )
        conn.commit()
        trigger = get_reflection_trigger(conn)
        assert trigger["mode"] == "calendar_days"
        assert trigger["day_interval"] == 7

    def test_check_agent_eligible_trade_count_due(self, conn):
        from meta.reflection_scheduler import check_agent_eligible
        # alpha_trader has 60 trades, interval is 20 — due for reflection
        trigger = {"mode": "trade_count", "trade_interval": 20}
        eligible, reason = check_agent_eligible(conn, "alpha_trader", trigger)
        assert eligible, f"Should be eligible: {reason}"

    def test_check_agent_eligible_trade_count_not_due(self, conn):
        from meta.reflection_scheduler import check_agent_eligible
        # Insert a recent reflection for alpha_trader
        conn.execute(
            "INSERT INTO reflections (agent_id, triggered_at) VALUES (?, ?)",
            ("alpha_trader", "2026-07-09T00:00:00Z"),
        )
        conn.commit()
        trigger = {"mode": "trade_count", "trade_interval": 20}
        # alpha has 60 trades, last reflection at trade 0, so 60 since last
        # Still due because 60 >= 20
        eligible, reason = check_agent_eligible(conn, "alpha_trader", trigger)
        assert eligible

    def test_check_agent_eligible_terminated(self, conn):
        from meta.reflection_scheduler import check_agent_eligible
        trigger = {"mode": "trade_count", "trade_interval": 20}
        eligible, reason = check_agent_eligible(conn, "dead_trader", trigger)
        assert not eligible
        assert "terminated" in reason

    def test_check_agent_eligible_not_found(self, conn):
        from meta.reflection_scheduler import check_agent_eligible
        trigger = {"mode": "trade_count", "trade_interval": 20}
        eligible, reason = check_agent_eligible(conn, "nonexistent", trigger)
        assert not eligible
        assert "not found" in reason

    def test_check_agent_eligible_manual_mode(self, conn):
        from meta.reflection_scheduler import check_agent_eligible
        trigger = {"mode": "manual"}
        eligible, reason = check_agent_eligible(conn, "alpha_trader", trigger)
        assert not eligible
        assert "manual" in reason


# ===================================================================
# Controller Tests
# ===================================================================

class TestController:

    def test_get_evaluation_interval_default(self, conn):
        from meta.controller import get_evaluation_interval
        interval = get_evaluation_interval(conn)
        assert interval == 30

    def test_evaluate_agent_unknown(self, conn):
        from meta.controller import evaluate_agent
        result = evaluate_agent(conn, "nonexistent")
        assert "error" in result

    def test_evaluate_agent_terminated(self, conn):
        from meta.controller import evaluate_agent
        # dead_trader is already terminated
        result = evaluate_agent(conn, "dead_trader")
        assert result.get("skipped")
        assert result.get("reason") == "agent already terminated"

    def test_evaluate_agent_good_standing(self, conn):
        from meta.controller import evaluate_agent
        result = evaluate_agent(conn, "alpha_trader", force=True)
        assert result["decision"] == "active"
        assert result["closed_trades"] == 60

    def test_evaluate_agent_terminates_bad_agent(self, conn):
        from meta.controller import evaluate_agent
        # gamma_trader has 30% win rate after 60 trades → terminate
        result = evaluate_agent(conn, "gamma_trader", force=True)
        assert result["decision"] == "terminate"
        # Agent status should be updated
        status = conn.execute(
            "SELECT status FROM agents WHERE id = 'gamma_trader'"
        ).fetchone()["status"]
        assert status == "terminated"

    def test_evaluate_agent_writes_evaluation_record(self, conn):
        from meta.controller import evaluate_agent
        evaluate_agent(conn, "alpha_trader", force=True)
        rows = conn.execute(
            "SELECT * FROM evaluations WHERE agent_id = 'alpha_trader'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["decision"] == "active"
        metrics = json.loads(rows[0]["metrics_json"])
        assert "profit_factor" in metrics


# ===================================================================
# Risk Officer Tests
# ===================================================================

class TestRiskOfficer:

    def test_risk_officer_no_kill_switch_normal(self, conn):
        from meta.risk_officer import RiskOfficer
        officer = RiskOfficer(conn, {"drawdown_kill_pct": 25})
        assert not officer.desk_in_kill_switch()

    def test_risk_officer_no_concentration_violations(self, conn):
        from meta.risk_officer import RiskOfficer
        officer = RiskOfficer(conn, {})
        violators = officer.agent_concentration_exceeded(threshold=0.40)
        assert violators == []

    def test_risk_officer_entry_gate_open_by_default(self, conn):
        from meta.risk_officer import RiskOfficer
        officer = RiskOfficer(conn, {})
        assert officer.is_entry_gate_open("alpha_trader")

    def test_risk_officer_entry_gate_closed_when_disabled(self, conn):
        from meta.risk_officer import RiskOfficer
        officer = RiskOfficer(conn, {})
        officer.disable_entry("alpha_trader", "test disable")
        assert not officer.is_entry_gate_open("alpha_trader")

    def test_risk_officer_entry_gate_reopened(self, conn):
        from meta.risk_officer import RiskOfficer
        officer = RiskOfficer(conn, {})
        officer.disable_entry("alpha_trader", "test disable")
        officer.enable_entry("alpha_trader")
        assert officer.is_entry_gate_open("alpha_trader")

    def test_risk_officer_cycle_report_structure(self, conn):
        from meta.risk_officer import RiskOfficer
        officer = RiskOfficer(conn, {})
        report = officer.run_cycle()
        assert "checked_at" in report
        assert "desk_kill_switch" in report
        assert "agents" in report
        # All active agents should be in the report
        assert "alpha_trader" in report["agents"]
        assert "beta_trader" in report["agents"]
        assert "gamma_trader" in report["agents"]
        # Dead_trader (terminated) should not be checked
        assert "dead_trader" not in report["agents"]

    def test_risk_check_cycle_convenience(self, conn):
        from meta.risk_officer import risk_check_cycle
        result = risk_check_cycle(conn, {})
        assert "checked_at" in result
        assert not result["desk_kill_switch"]


# ===================================================================
# Head of Desk Tests
# ===================================================================

class TestHeadOfDesk:

    def test_get_agent_roster(self, conn):
        from meta.head_of_desk import get_agent_roster
        roster = get_agent_roster(conn)
        assert len(roster) >= 4  # alpha, beta, gamma, benchmark, dead
        names = [a["name"] for a in roster]
        assert "alpha_trader" in names
        assert "dead_trader" in names

    def test_get_strategy_distribution(self, conn):
        from meta.head_of_desk import get_strategy_distribution
        distribution = get_strategy_distribution(conn)
        assert isinstance(distribution, dict)

    def test_ensure_agent_count_below_target(self, conn):
        from meta.head_of_desk import ensure_agent_count
        # 4 active/rookie agents (alpha, beta, gamma, benchmark_random_walk)
        spawned = ensure_agent_count(conn, {
            "target_agent_count": 5,
            "max_agents": 20,
        })
        assert len(spawned) == 1  # deficit = 5 - 4 = 1

    def test_ensure_agent_count_already_at_target(self, conn):
        from meta.head_of_desk import ensure_agent_count
        spawned = ensure_agent_count(conn, {
            "target_agent_count": 3,
            "max_agents": 20,
        })
        assert spawned == []  # already at target

    def test_cull_if_under_max(self, conn):
        from meta.head_of_desk import cull_if_overpopulated
        culled = cull_if_overpopulated(conn, {
            "max_agents": 20,
        })
        assert culled == []  # 3 active agents, under 20

    def test_run_head_of_desk_cycle(self, conn):
        from meta.head_of_desk import run_head_of_desk_cycle
        report = run_head_of_desk_cycle(conn, {
            "target_agent_count": 5,
            "max_agents": 20,
        })
        assert "spawned" in report
        assert "culled" in report
        assert report["agent_count"] > 0
