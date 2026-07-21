"""seed_benchmarks must create an initial paper account snapshot for each
benchmark agent.

benchmark_btc_hold never trades on its own schedule, and account rows were
previously only created by the trading path — so it had no accounts row,
was excluded from capture_equity_snapshot(), and its trader page could
never show an equity curve.
"""
from scripts.seed_benchmarks import seed_benchmark_agents
from store.db import capture_equity_snapshot

CONFIG = {"desk": {"starting_balance": 50000.0}}


def test_seed_creates_account_rows_for_both_benchmarks(conn):
    seed_benchmark_agents(conn, CONFIG)
    for agent_id in ("benchmark_random_walk", "benchmark_btc_hold"):
        row = conn.execute(
            "SELECT balance, peak_balance FROM accounts "
            "WHERE agent_id = ? AND mode = 'paper'",
            (agent_id,),
        ).fetchone()
        assert row is not None, f"{agent_id} has no account row"
        assert row["balance"] == 50000.0
        assert row["peak_balance"] == 50000.0


def test_seed_is_idempotent_for_accounts(conn):
    seed_benchmark_agents(conn, CONFIG)
    seed_benchmark_agents(conn, CONFIG)
    n = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE agent_id = 'benchmark_btc_hold'"
    ).fetchone()[0]
    assert n == 1


def test_seed_does_not_reset_existing_balance(conn):
    seed_benchmark_agents(conn, CONFIG)
    conn.execute(
        "UPDATE accounts SET balance = 41234.5 WHERE agent_id = 'benchmark_btc_hold'"
    )
    conn.commit()
    seed_benchmark_agents(conn, CONFIG)
    row = conn.execute(
        "SELECT balance FROM accounts WHERE agent_id = 'benchmark_btc_hold' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["balance"] == 41234.5


def test_benchmarks_included_in_equity_snapshot(conn):
    seed_benchmark_agents(conn, CONFIG)
    capture_equity_snapshot(conn)
    agents = {
        r["agent_id"]
        for r in conn.execute("SELECT DISTINCT agent_id FROM agent_equity_snapshots")
    }
    assert "benchmark_btc_hold" in agents
    assert "benchmark_random_walk" in agents
