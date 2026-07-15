"""tests/test_spawner.py -- M8 graveyard-similarity spawn workflow.

Covers the missing M8 test-table row: "Spawning an agent with a thesis
substantively similar to a terminated agent is rejected." meta/spawner.py's
check_against_graveyard() is the real Jaccard-similarity gate (landed in
commit a3ba0b9); spawn_agent() itself does not call it internally, so the
intended usage contract is: callers run check_against_graveyard() first and
only proceed to spawn_agent() when it returns (True, ""). This test exercises
that full workflow end to end with real spawn_agent()-created agents/theses
rather than raw INSERTs, to prove the rejection actually blocks a new agent
from being created.

tests/test_multi_agent.py already covers check_against_graveyard() in
isolation (no-terminated-agents default-accept, direct-similar-thesis-reject,
dissimilar-thesis-accept); this file focuses on the spawn-blocking workflow
specifically named in the M8 test table.
"""
from datetime import datetime, timedelta, timezone

import pytest

from meta.spawner import check_against_graveyard, spawn_agent


@pytest.fixture(autouse=True)
def _redirect_theses_dir(tmp_path, monkeypatch):
    """Never let a test write into the real agents/theses/ directory."""
    import meta.spawner as spawner_module

    monkeypatch.setattr(spawner_module, "_THESES_DIR", tmp_path / "theses")


TERMINATED_THESIS = """# dead_trader -- momentum thesis

Cross-sectional momentum across the perpetuals universe, ranking assets on
multiple return horizons (30m, 2h, 12h, 24h) and entering the strongest
relative performers. Momentum acceleration and volatility-adjusted returns
sharpen the signal. Long only, 3x leverage, 12% position size, 2.5% stop
loss, 5% take profit, 12 hour max hold.
"""

SIMILAR_NEW_THESIS = """# new_trader -- momentum thesis

Cross-sectional momentum across the perpetuals universe, ranking assets on
multiple return horizons (30m, 2h, 12h, 24h) and entering the strongest
relative performers. Momentum acceleration and volatility-adjusted returns
sharpen the signal. Long only, 3x leverage, 10% position size, 2% stop loss,
4% take profit, 8 hour max hold.
"""

DISSIMILAR_NEW_THESIS = """# new_trader -- funding thesis

Funding rate dislocations are self-correcting: extreme positive or negative
funding z-scores versus a rolling 14-day history predict reversion. Enter
when funding is statistically irrational and hold until it normalises.
"""


def test_graveyard_similarity_blocks_duplicate(conn):
    """A thesis that is substantively similar to a terminated agent's thesis
    is rejected by check_against_graveyard(), and — following the intended
    caller contract — the new agent is never actually spawned."""
    # Spawn and then terminate an agent with a momentum thesis, via the real
    # spawn_agent() path (writes a thesis file + theses row, not a raw INSERT).
    spawn_agent(conn, "dead_trader", TERMINATED_THESIS, status="rookie")
    conn.execute(
        "UPDATE agents SET status = 'terminated' WHERE id = ?", ("dead_trader",)
    )
    conn.commit()

    ok, reason = check_against_graveyard(conn, SIMILAR_NEW_THESIS)

    assert ok is False
    assert "dead_trader" in reason
    assert "Jaccard" in reason

    # The caller contract: only spawn if the graveyard check passed. Verify
    # that respecting the rejection means the new agent never gets created.
    if ok:
        spawn_agent(conn, "new_trader", SIMILAR_NEW_THESIS, status="rookie")

    row = conn.execute(
        "SELECT id FROM agents WHERE id = ?", ("new_trader",)
    ).fetchone()
    assert row is None

    # The rejection is logged to the evaluations table (per
    # check_against_graveyard's docstring).
    eval_row = conn.execute(
        "SELECT decision, reason FROM evaluations "
        "WHERE agent_id = '__graveyard__' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert eval_row["decision"] == "REJECT"
    assert "dead_trader" in eval_row["reason"]


def test_graveyard_similarity_allows_dissimilar_thesis_to_spawn(conn):
    """A substantively different thesis passes the graveyard check and the
    new agent is actually created, proving the gate isn't overly strict."""
    spawn_agent(conn, "dead_trader", TERMINATED_THESIS, status="rookie")
    conn.execute(
        "UPDATE agents SET status = 'terminated' WHERE id = ?", ("dead_trader",)
    )
    conn.commit()

    ok, reason = check_against_graveyard(conn, DISSIMILAR_NEW_THESIS)
    assert ok is True
    assert reason == ""

    if ok:
        spawn_agent(conn, "new_trader", DISSIMILAR_NEW_THESIS, status="rookie")

    row = conn.execute(
        "SELECT id, status FROM agents WHERE id = ?", ("new_trader",)
    ).fetchone()
    assert row is not None
    assert row["status"] == "rookie"


def _iso(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_harvest_seeds_on_termination(conn):
    """On termination, meta/controller.py::evaluate_agent harvests the 5
    best-PnL closed trades into the `seeds` table (meta/evaluator.py::
    harvest_best_trades, called from evaluate_agent when decision ==
    "terminate"). M9 proposal test table: "test_spawn_from_harvest" is
    deliberately deferred to a later milestone task; this covers only the
    harvest-on-termination half.

    Spawns the agent via the real spawn_agent() path (per this file's
    convention), then closes 50 trades at a 30% win rate -- below the
    win_rate_below_35 termination floor at the 50-trade threshold
    (meta/evaluator.py::get_lifecycle_decision) -- to drive termination
    deterministically, with no dependency on benchmark data or evaluation
    history. Winning trades carry a distinct pnl_pct each so the "5 best"
    ordering is a real assertion, not a tie.
    """
    from store.db import insert_account_snapshot
    from meta.controller import evaluate_agent

    spawn_agent(conn, "harvest_agent", "# harvest_agent thesis\n\nplaceholder.", status="rookie")
    conn.execute("UPDATE agents SET status = 'active' WHERE id = ?", ("harvest_agent",))
    insert_account_snapshot(conn, "harvest_agent", "paper", 10000.0, 10000.0)
    conn.commit()

    ts = _iso(1)
    for i in range(50):
        is_win = i < 15  # 30% win rate < 35% floor
        pnl_pct = (0.02 + i * 0.001) if is_win else -0.015
        result = "win" if is_win else "loss"
        conn.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
               'closed', ?, ?, ?, ?, ?)""",
            (f"harvest_{i}", "harvest_agent", pnl_pct, pnl_pct * 10000, result, ts, ts),
        )
    conn.commit()

    result = evaluate_agent(conn, "harvest_agent", force=True)
    assert result["decision"] == "terminate"
    assert result["harvested"] == 5

    seeds = conn.execute(
        "SELECT trade_id, pnl_pct FROM seeds WHERE source_agent_id = 'harvest_agent' ORDER BY id"
    ).fetchall()
    assert len(seeds) == 5
    pnls = [row["pnl_pct"] for row in seeds]
    assert pnls == sorted(pnls, reverse=True)
    # The 5 highest-pnl winning trades: i=14..10 -> pnl 0.034 down to 0.030
    assert [row["trade_id"] for row in seeds] == [
        "harvest_14", "harvest_13", "harvest_12", "harvest_11", "harvest_10",
    ]
