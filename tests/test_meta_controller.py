"""tests/test_meta_controller.py — M9 proposal test table, criteria 4/5/6.

Named tests per the proposal's meta-controller test table. meta/evaluator.py
and meta/controller.py are the real M9 lifecycle-decision code (untouched by
this file); each fixture below is built to isolate exactly one lifecycle
rule so the test proves that rule, not an accident of ordering.

get_lifecycle_decision() (meta/evaluator.py) evaluates rules in this order,
and the first match wins:
  1. zero_trades_5d      — no closed trade in the last 5 days -> "review"
  2. win_rate_below_35   — win_rate < 0.35 and closed_trades >= 50 -> "terminate"
  3. drawdown_exceeds_20pct — max drawdown > 20% -> "suspend"
  4. pf_below_08_2eval   — PF < 0.8 in this AND the prior evaluation -> "suspend"
     (requires 2+ rows in `evaluations`; never fires on an agent's first eval)
  5. not_beating_null_50 / not_beating_null_100 — significance test vs.
     benchmark_random_walk, gated on closed_trades >= 50 and a valid null
     (benchmark_random_walk itself has >= 30 closed trades)
  6. suspended-agent restore-or-terminate (only when agent.status == "suspended")
  7. default -> "active"

Every fixture here seeds trades with entry_timestamp relative to
datetime.now() (never a hardcoded literal date) — see tests/test_m9_modules.py
_iso()'s docstring for why a fixed date is a landmine against rule 1.

Numeric fixture values below were derived empirically against the real
compute_metrics()/get_lifecycle_decision() (not hand-computed) to land in
the intended branch; see each test's comment for the specific rule targeted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from store.db import insert_account_snapshot
from store.performance import compute_metrics
from meta.controller import evaluate_agent


def _iso(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _seed_agent(conn, agent_id, status="active", balance=10000.0, peak_balance=10000.0):
    """Minimal agent + account row, patterned on test_m9_modules.py's _seed_agents."""
    conn.execute(
        "INSERT INTO agents (id, name, status, spawn_date, config_json) VALUES (?, ?, ?, ?, ?)",
        (agent_id, agent_id, status, _iso(2000), "{}"),
    )
    insert_account_snapshot(conn, agent_id, "paper", balance, peak_balance)


def _seed_trades(conn, agent_id, n, n_win, win_pnl_pct, loss_pnl_pct, hours_ago=1):
    """n closed trades (first n_win are wins), patterned on test_m9_modules.py's
    _seed_trades. All trades share the same entry_timestamp (hours_ago relative
    to now) unless the caller needs staleness, in which case they pass a large
    hours_ago (see test_zero_trade_review)."""
    ts = _iso(hours_ago)
    for i in range(n):
        is_win = i < n_win
        pnl_pct = win_pnl_pct if is_win else loss_pnl_pct
        result = "win" if is_win else "loss"
        conn.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
               'closed', ?, ?, ?, ?, ?)""",
            (f"{agent_id}_{i}", agent_id, pnl_pct, pnl_pct * 10000, result, ts, ts),
        )
    conn.commit()


def test_evaluation_runs_on_schedule(conn):
    """The meta-controller job fires when completed_trades is a multiple of
    the configured trade interval (default 30).

    30 closed trades with no prior evaluation row: evaluate_agent()'s
    cadence gate computes closed_trades % 30 == 0, so evaluation proceeds.
    """
    _seed_agent(conn, "sched_agent")
    _seed_trades(conn, "sched_agent", n=30, n_win=18, win_pnl_pct=0.02, loss_pnl_pct=-0.015)

    result = evaluate_agent(conn, "sched_agent")

    assert not result.get("skipped"), result
    assert result["closed_trades"] == 30
    rows = conn.execute(
        "SELECT * FROM evaluations WHERE agent_id = 'sched_agent'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["trades_evaluated"] == 30


def test_cull_below_null(conn):
    """An agent that fails to beat the null after 100 trades is terminated
    (trigger: not_beating_null_100).

    Rule precedence note: the "not_beating_null" block checks the 50-trade
    suspend branch before the 100-trade terminate branch, and the 50-trade
    branch fires whenever profit_factor < 1.0 -- so a PF < 1.0 agent would be
    SUSPENDED at the 50-trade check before ever reaching the 100-trade
    terminate check, even at 100 trades. To isolate the terminate branch, the
    agent's profit_factor must be >= 1.0 while still not "beating null"
    (beats_null requires positive sharpe_diff over the benchmark) -- 45%
    win rate at +1.25%/-1.0% wins/losses gives PF~1.02 with sharpe ~0.01,
    well under the benchmark's sharpe (~0.64) from its steadier 70% win rate.

    The proposal's "p < 0.05" framing doesn't map onto this code: p_value_estimate
    only classifies how significantly the agent BEATS null (t_stat thresholds
    at 1.96/1.28); a losing agent's t_stat is negative and is bucketed as
    ">0.10" regardless of margin. What actually gates termination here is
    beats_null=False + null_valid (benchmark has >=30 trades), not a p<0.05
    read on the underperformance itself.
    """
    _seed_agent(conn, "benchmark_random_walk")
    _seed_agent(conn, "null_test_agent")
    _seed_trades(
        conn, "benchmark_random_walk", n=30, n_win=21,
        win_pnl_pct=0.006, loss_pnl_pct=-0.004, hours_ago=2,
    )
    _seed_trades(
        conn, "null_test_agent", n=100, n_win=45,
        win_pnl_pct=0.0125, loss_pnl_pct=-0.01, hours_ago=1,
    )

    metrics = compute_metrics(conn, "null_test_agent")
    assert metrics["profit_factor"] >= 1.0  # confirms the 50-trade suspend branch is bypassed

    result = evaluate_agent(conn, "null_test_agent", force=True)

    assert result["decision"] == "terminate"
    assert result["trigger"] == "not_beating_null_100"
    status = conn.execute(
        "SELECT status FROM agents WHERE id = 'null_test_agent'"
    ).fetchone()["status"]
    assert status == "terminated"


def test_probation_suspension(conn):
    """An agent at PF ~0.85 after 60 trades is SUSPENDED, not terminated
    (trigger: not_beating_null_50).

    Not the same mechanism as the proposal's literal "PF < 0.8 two
    consecutive evaluations" rule (pf_below_08_2eval), which requires
    evaluation history that doesn't exist on an agent's first evaluation
    and wouldn't fire at PF=0.85 anyway (that rule's own threshold is 0.8).
    At 60 trades (>=50, <100) with PF < 1.0 and a valid null, the
    not_beating_null block's 50-trade branch suspends before the
    100-trade branch is ever reached (60 < 100) -- so this agent is
    suspended on its first evaluation purely from underperforming null,
    which is what "probation" means operationally here.
    """
    _seed_agent(conn, "benchmark_random_walk")
    _seed_agent(conn, "probation_agent")
    _seed_trades(
        conn, "benchmark_random_walk", n=30, n_win=21,
        win_pnl_pct=0.006, loss_pnl_pct=-0.004, hours_ago=2,
    )
    _seed_trades(
        conn, "probation_agent", n=60, n_win=27,
        win_pnl_pct=0.02, loss_pnl_pct=-0.019251336898395723, hours_ago=1,
    )

    metrics = compute_metrics(conn, "probation_agent")
    assert metrics["profit_factor"] == pytest.approx(0.85, abs=0.005)

    result = evaluate_agent(conn, "probation_agent", force=True)

    assert result["decision"] == "suspend"
    assert result["trigger"] == "not_beating_null_50"
    status = conn.execute(
        "SELECT status FROM agents WHERE id = 'probation_agent'"
    ).fetchone()["status"]
    assert status == "suspended"


def test_cull_on_drawdown(conn):
    """An agent with >20% max drawdown is immediately suspended (trigger:
    drawdown_exceeds_20pct), independent of win rate / trade count / null.

    Drawdown is derived from the latest accounts row (peak_balance vs.
    balance), not from the trades table, and the rule is checked before any
    trade-count-gated rule -- 5 trades is enough to avoid the (higher-count)
    win_rate_below_35 and not_beating_null branches entirely.
    """
    _seed_agent(conn, "dd_agent", balance=7500.0, peak_balance=10000.0)  # 25% dd
    _seed_trades(conn, "dd_agent", n=5, n_win=3, win_pnl_pct=0.02, loss_pnl_pct=-0.015)

    result = evaluate_agent(conn, "dd_agent", force=True)

    assert result["decision"] == "suspend"
    assert result["trigger"] == "drawdown_exceeds_20pct"
    status = conn.execute(
        "SELECT status FROM agents WHERE id = 'dd_agent'"
    ).fetchone()["status"]
    assert status == "suspended"


def test_cull_on_win_rate(conn):
    """An agent with <35% win rate after 50 trades is terminated (trigger:
    win_rate_below_35). This rule is checked immediately after the
    zero-trades-5d pre-check and before drawdown/PF/null rules, so it fires
    regardless of any of those -- no benchmark_random_walk agent is needed.
    """
    _seed_agent(conn, "wr_agent")
    _seed_trades(conn, "wr_agent", n=50, n_win=15, win_pnl_pct=0.02, loss_pnl_pct=-0.015)

    result = evaluate_agent(conn, "wr_agent", force=True)

    assert result["decision"] == "terminate"
    assert result["trigger"] == "win_rate_below_35"
    status = conn.execute(
        "SELECT status FROM agents WHERE id = 'wr_agent'"
    ).fetchone()["status"]
    assert status == "terminated"


def test_zero_trade_review(conn):
    """An agent with no trades in the last 5 days triggers a thesis-review
    flag (trigger: zero_trades_5d), decision "review" (not suspend/terminate
    -- agent status is left unchanged).

    Precedence nuance: get_lifecycle_decision's pre-check is guarded by
    `if total_trades > 0`, so an agent with LITERALLY zero trades ever
    skips this check entirely and falls through to the other rules (most of
    which are no-ops at 0 trades) to "active". The proposal's "0 trades"
    really means "0 trades in the trailing 5-day window" -- an agent that
    HAS traded before but has gone stale -- which is what this fixture
    seeds: 10 closed trades, all 6 days old.
    """
    _seed_agent(conn, "stale_agent")
    _seed_trades(
        conn, "stale_agent", n=10, n_win=6,
        win_pnl_pct=0.02, loss_pnl_pct=-0.015, hours_ago=6 * 24,
    )

    result = evaluate_agent(conn, "stale_agent", force=True)

    assert result["decision"] == "review"
    assert result["trigger"] == "zero_trades_5d"
    status = conn.execute(
        "SELECT status FROM agents WHERE id = 'stale_agent'"
    ).fetchone()["status"]
    assert status == "active"  # review does not change agent status


def test_review_required_flag_set(conn):
    """When zero_trades_5d fires, the evaluations row has review_required=1."""
    _seed_agent(conn, "review_flag_agent")
    _seed_trades(
        conn, "review_flag_agent", n=10, n_win=6,
        win_pnl_pct=0.02, loss_pnl_pct=-0.015, hours_ago=6 * 24,
    )

    result = evaluate_agent(conn, "review_flag_agent", force=True)
    assert result["decision"] == "review"

    row = conn.execute(
        "SELECT review_required FROM evaluations"
        " WHERE agent_id = 'review_flag_agent'"
    ).fetchone()
    assert row is not None
    assert row["review_required"] == 1


def test_review_required_not_set_for_active(conn):
    """An active agent's evaluation has review_required=0."""
    _seed_agent(conn, "active_flag_agent")
    _seed_trades(
        conn, "active_flag_agent", n=30, n_win=18,
        win_pnl_pct=0.02, loss_pnl_pct=-0.015,
    )

    result = evaluate_agent(conn, "active_flag_agent", force=True)
    assert result["decision"] == "active"

    row = conn.execute(
        "SELECT review_required FROM evaluations"
        " WHERE agent_id = 'active_flag_agent'"
    ).fetchone()
    assert row is not None
    assert row["review_required"] == 0


def test_evaluation_skipped_off_cadence(conn):
    """Evaluation is skipped when completed_trades is not a multiple of the
    interval (e.g. 35 trades with default interval 30)."""
    _seed_agent(conn, "off_cadence_agent")
    _seed_trades(
        conn, "off_cadence_agent", n=35, n_win=21,
        win_pnl_pct=0.02, loss_pnl_pct=-0.015,
    )

    result = evaluate_agent(conn, "off_cadence_agent")
    assert result.get("skipped")
    assert "cadence" in result["reason"]


def test_pf_below_08_two_consecutive(conn):
    """PF < 0.8 in two consecutive evaluations triggers SUSPENDED.

    Three evaluations are needed: the third sees two prior evaluations both
    with PF < 0.8, firing the pf_below_08_2eval trigger.
    """
    _seed_agent(conn, "pf_agent")
    # Eval 1: PF ~0.53, 30 trades
    _seed_trades(
        conn, "pf_agent", n=30, n_win=12,
        win_pnl_pct=0.02, loss_pnl_pct=-0.025,
    )
    result1 = evaluate_agent(conn, "pf_agent", force=True)
    assert result1["decision"] == "active"

    # Eval 2: 60 trades, same profile, PF still < 0.8
    for i in range(30, 60):
        is_win = (i - 30) < 12
        pnl_pct = 0.02 if is_win else -0.025
        result = "win" if is_win else "loss"
        conn.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
               'closed', ?, ?, ?, ?, ?)""",
            (f"pf_agent_{i}", "pf_agent", pnl_pct, pnl_pct * 10000, result,
             _iso(0.5), _iso(0.5)),
        )
    conn.commit()
    result2 = evaluate_agent(conn, "pf_agent", force=True)
    assert result2["decision"] == "active"

    # Eval 3: 90 trades, PF still < 0.8 — now 2 prior evals exist
    for i in range(60, 90):
        is_win = (i - 60) < 12
        pnl_pct = 0.02 if is_win else -0.025
        result = "win" if is_win else "loss"
        conn.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
               'closed', ?, ?, ?, ?, ?)""",
            (f"pf_agent_{i}", "pf_agent", pnl_pct, pnl_pct * 10000, result,
             _iso(0.5), _iso(0.5)),
        )
    conn.commit()
    result3 = evaluate_agent(conn, "pf_agent", force=True)
    assert result3["decision"] == "suspend"
    assert result3["trigger"] == "pf_below_08_2eval"


def test_harvest_inserts_key_conditions_met(conn):
    """Seeds table receives key_conditions_met from the trade row."""
    _seed_agent(conn, "kc_agent")
    ts = _iso(1)
    conn.execute(
        """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
           leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
           key_conditions_met, hypothesis)
           VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
           'closed', 0.05, 500.0, 'win', ?, ?, ?, ?)""",
        ("kc_trade_1", "kc_agent", ts, ts, "volume_spike;funding_flip", "breakout thesis"),
    )
    conn.execute(
        """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
           leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
           hypothesis)
           VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
           'closed', 0.03, 300.0, 'win', ?, ?, ?)""",
        ("kc_trade_2", "kc_agent", ts, ts, "breakout thesis v2"),
    )
    conn.commit()

    from meta.evaluator import harvest_best_trades
    harvest_best_trades(conn, "kc_agent", count=2)

    rows = conn.execute(
        "SELECT trade_id, key_conditions_met FROM seeds WHERE agent_id = 'kc_agent' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    # Trade with key_conditions_met should be first (cleanest execution)
    assert rows[0]["trade_id"] == "kc_trade_1"
    assert rows[0]["key_conditions_met"] == "volume_spike;funding_flip"
    assert rows[1]["trade_id"] == "kc_trade_2"
    assert rows[1]["key_conditions_met"] is None
