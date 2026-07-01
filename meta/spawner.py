"""meta/spawner.py — Agent spawning and name generation."""

import random
from datetime import datetime, timezone
from pathlib import Path

_THESES_DIR = Path(__file__).parent.parent / "agents" / "theses"

# Preset list matching the proposal's seed names — these are claimed at
# spawn time so generate_agent_name skips them.
_RESERVED_NAMES: set[str] = {
    "iron_moth", "jade_hawk", "silver_basin", "copper_vane",
    "gray_finch", "amber_wolf", "steel_crane", "onyx_heron",
}

_ADJECTIVES = [
    "amber", "steel", "onyx", "jade", "silver", "copper", "gray", "iron",
    "crimson", "emerald", "sapphire", "golden", "bronze", "scarlet", "ivory",
    "azure", "cobalt", "violet", "coral", "crystal", "frost", "shadow",
    "storm", "thunder", "echo", "phantom", "polar", "dark", "dawn", "lunar",
]

_ANIMALS = [
    "moth", "hawk", "basin", "vane", "finch", "wolf", "crane", "heron",
    "fox", "owl", "raven", "stag", "bear", "serpent", "phoenix", "tiger",
    "lion", "falcon", "viper", "puma", "jackal", "osprey", "badger", "kestrel",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_agent_name(conn, used_names: set[str] | None = None) -> str:
    """Generate a unique two-word agent name (adjective_animal).

    Checks the SQLite agents table for existing names and the optional
    used_names set for in-memory dedup (e.g. during bulk seed).
    """
    existing = {
        row[0] for row in conn.execute("SELECT name FROM agents").fetchall()
    }
    taken = existing | (used_names or set())

    for adj in _ADJECTIVES:
        for animal in _ANIMALS:
            name = f"{adj}_{animal}"
            if name not in taken:
                return name
    raise RuntimeError("No unused name combinations available")


def spawn_agent(
    conn,
    name: str,
    seed_thesis_text: str,
    status: str = "rookie",
    config_overrides: dict | None = None,
    starting_balance: float = 50000.0,
) -> dict:
    """Create a new agent record in SQLite, write initial thesis, return agent dict.

    Parameters
    ----------
    conn : sqlite3.Connection
        Active DB connection.
    name : str
        Two-word agent name (e.g. 'iron_moth').
    seed_thesis_text : str
        The initial thesis / seed hypothesis for this agent.
    status : str
        Initial status (rookie, active, etc.). Defaults to 'rookie'.
    config_overrides : dict | None
        Per-agent config overrides (e.g. {"wake_interval": 90}).
    starting_balance : float
        Initial account balance.

    Returns the agent row as a dict.
    """
    config_json = _serialise_config(config_overrides or {})
    now = _now()

    thesis_version = 1  # new agents always start at v1

    conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, status, spawn_date, config_json, current_thesis_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, name, status, now, config_json, thesis_version),
    )
    conn.commit()

    # Write thesis file
    _THESES_DIR.mkdir(parents=True, exist_ok=True)
    thesis_file = _THESES_DIR / f"{name}_v{thesis_version}.md"
    thesis_file.write_text(seed_thesis_text, encoding="utf-8")

    # Insert thesis record
    conn.execute(
        "INSERT INTO theses (agent_id, version, text, created_at) VALUES (?, ?, ?, ?)",
        (name, thesis_version, seed_thesis_text, now),
    )

    # Create account snapshot
    from store.db import insert_account_snapshot
    insert_account_snapshot(conn, name, "paper", starting_balance, starting_balance)

    conn.commit()

    row = conn.execute("SELECT * FROM agents WHERE id = ?", (name,)).fetchone()
    return dict(row) if row else {"id": name, "name": name, "status": status}


def check_against_graveyard(conn, thesis_text: str) -> bool:
    """Check if a thesis is substantively similar to a terminated agent's thesis.

    For now, always returns True (unique — no similar thesis found).
    A real implementation would do an LLM similarity check against terminated
    agents' thesis texts stored in the `theses` table.
    """
    return True


def _serialise_config(overrides: dict) -> str:
    import json
    return json.dumps(overrides)
