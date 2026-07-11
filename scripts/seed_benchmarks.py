"""
scripts/seed_benchmarks.py — Seed benchmark agents for leaderboard comparison.

M6 Task 7: Seed `random_walk` and `btc_hold` benchmark agents.
These agents serve as baselines for evaluating the AI agents' performance.

- random_walk: Makes random long/short decisions with random entry/exit times
- btc_hold: Only trades BTC, holds positions indefinitely (HODL benchmark)
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_THESES_DIR = Path(__file__).parent.parent / "agents" / "theses"


def _seal_thesis(agent_id: str, text: str) -> None:
    """Write a thesis file for a benchmark agent so forge.py startup doesn't
    log spurious 'Thesis not found' warnings."""
    _THESES_DIR.mkdir(parents=True, exist_ok=True)
    thesis_path = _THESES_DIR / f"{agent_id}_v1.md"
    if not thesis_path.exists():
        thesis_path.write_text(text, encoding="utf-8")


def seed_benchmark_agents(conn: sqlite3.Connection, config: dict) -> None:
    """Seed benchmark agents into the database."""
    desk_config = config.get("desk", {})
    starting_balance = desk_config.get("starting_balance", 50000.0)
    universe = config.get("universe", ["BTC-PERP", "ETH-PERP", "SOL-PERP"])

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Seed random_walk agent
    random_walk_config = {
        "pinned_model": None,
        "benchmark_type": "random_walk",
        "description": "Random walk benchmark — makes random trading decisions",
        "starting_balance": starting_balance,
        "universe": universe,
    }

    conn.execute(
        """INSERT OR IGNORE INTO agents (id, name, status, spawn_date, config_json, current_thesis_version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "benchmark_random_walk",
            "random_walk",
            "active",
            now,
            json.dumps(random_walk_config),
            1,
        ),
    )
    _seal_thesis(
        "benchmark_random_walk",
        "# Random Walk Benchmark\n\n"
        "Makes random long/short decisions with random entry/exit times. "
        "Serves as a baseline for evaluating AI agent performance.",
    )

    # Seed btc_hold agent
    btc_hold_config = {
        "pinned_model": None,
        "benchmark_type": "btc_hold",
        "description": "BTC hold benchmark — only trades BTC, holds positions indefinitely",
        "preferred_asset": "BTC-PERP",
        "starting_balance": starting_balance,
        "universe": universe,
    }

    conn.execute(
        """INSERT OR IGNORE INTO agents (id, name, status, spawn_date, config_json, current_thesis_version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "benchmark_btc_hold",
            "btc_hold",
            "active",
            now,
            json.dumps(btc_hold_config),
            1,
        ),
    )
    _seal_thesis(
        "benchmark_btc_hold",
        "# BTC Hold Benchmark\n\n"
        "Only trades BTC-PERP, holds positions indefinitely (HODL benchmark). "
        "Serves as a baseline for evaluating AI agent performance.",
    )

    conn.commit()
    print("Seeded benchmark agents: random_walk, btc_hold")


if __name__ == "__main__":
    from pathlib import Path

    import yaml

    config_path = Path("config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    conn = sqlite3.connect("data/forge.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    seed_benchmark_agents(conn, config)
    conn.close()
