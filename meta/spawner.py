"""meta/spawner.py — Agent spawning and name generation."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_THESES_DIR = Path(__file__).parent.parent / "agents" / "theses"

# Preset list matching the proposal's seed names — these are claimed at
# spawn time so generate_agent_name skips them.
_RESERVED_NAMES: set[str] = {
    "iron_moth",
    "jade_hawk",
    "silver_basin",
    "copper_vane",
    "gray_finch",
    "amber_wolf",
    "steel_crane",
    "onyx_heron",
    "violet_lion",
    "crimson_fox",
    "teal_wolf",
    "cobalt_falcon",
    "ruby_serpent",
    "golden_phoenix",
    "ivory_dragon",
    "obsidian_panther",
    "sage_turtle",
    "scarlet_hawk",
    "emerald_owl",
    "bronze_stag",
    "sapphire_raven",
    "coral_viper",
    "frost_bear",
    "storm_kestrel",
    "lunar_badger",
    "echo_jackal",
}

_ADJECTIVES = [
    "amber",
    "steel",
    "onyx",
    "jade",
    "silver",
    "copper",
    "gray",
    "iron",
    "crimson",
    "emerald",
    "sapphire",
    "golden",
    "bronze",
    "scarlet",
    "ivory",
    "azure",
    "cobalt",
    "violet",
    "coral",
    "crystal",
    "frost",
    "shadow",
    "storm",
    "thunder",
    "echo",
    "phantom",
    "polar",
    "dark",
    "dawn",
    "lunar",
    "teal",
    "ruby",
    "sage",
    "mauve",
    "indigo",
    "pearl",
    "opal",
    "rust",
    "slate",
    "umber",
    "celadon",
    "smoke",
    "blush",
    "moss",
    "flint",
]

_ANIMALS = [
    "moth",
    "hawk",
    "basin",
    "vane",
    "finch",
    "wolf",
    "crane",
    "heron",
    "fox",
    "owl",
    "raven",
    "stag",
    "bear",
    "serpent",
    "phoenix",
    "tiger",
    "lion",
    "falcon",
    "viper",
    "puma",
    "jackal",
    "osprey",
    "badger",
    "kestrel",
    "turtle",
    "dragon",
    "panther",
    "skua",
    "bison",
    "cobra",
    "lynx",
    "marten",
    "sable",
    "rook",
    "shrike",
    "cod",
    "loon",
    "coot",
    "dove",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_agent_name(conn, used_names: set[str] | None = None) -> str:
    """Generate a unique two-word agent name (adjective_animal).

    Checks the SQLite agents table for existing names and the optional
    used_names set for in-memory dedup (e.g. during bulk seed).
    """
    existing = {row[0] for row in conn.execute("SELECT name FROM agents").fetchall()}
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

    cursor = conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, status, spawn_date, config_json, current_thesis_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, name, status, now, config_json, thesis_version),
    )
    conn.commit()

    if cursor.rowcount == 0:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (name,)).fetchone()
        return dict(row) if row else {"id": name, "name": name, "status": status}

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


def _tokenize(text: str) -> list[str]:
    """Tokenize thesis text into lowercase word tokens (3+ characters)."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= 3]


def check_against_graveyard(
    conn, thesis_text: str, config: dict | None = None
) -> tuple[bool, str]:
    """Check if a thesis is substantively similar to any terminated agent's thesis.

    Uses Jaccard similarity on word-level token sets with TF-IDF-like weighting
    via frequency filtering: words appearing in >80% of terminated theses are
    treated as corpus-level stop words (only when >=3 terminated theses exist).

    The comparison result is logged to the ``evaluations`` table.

    Parameters
    ----------
    conn : sqlite3.Connection
        Active DB connection.
    thesis_text : str
        The proposed thesis text to check.
    config : dict or None
        Optional config dict; may contain ``"graveyard_similarity_threshold"``
        (default 0.45).

    Returns
    -------
    tuple[bool, str]
        ``(True, "")`` if the thesis is unique enough,
        ``(False, "reason...")`` if too similar to a terminated agent's thesis.
    """
    if config is None:
        config = {}
    threshold = float(config.get("graveyard_similarity_threshold", 0.45))

    # Fetch latest thesis for every terminated agent
    rows = conn.execute("""
        SELECT a.name AS agent_name, t.text AS thesis_text
        FROM agents a
        JOIN theses t ON t.agent_id = a.id
        WHERE a.status = 'terminated'
          AND t.version = a.current_thesis_version
    """).fetchall()

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # --- No terminated agents → accept by default ---
    if not rows:
        conn.execute(
            "INSERT OR IGNORE INTO agents "
            "(id, name, status, spawn_date, config_json, current_thesis_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("__graveyard__", "__graveyard__", "system", now, "{}", 0),
        )
        conn.execute(
            "INSERT INTO evaluations (agent_id, evaluated_at, trades_evaluated, "
            "metrics_json, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "__graveyard__",
                now,
                0,
                json.dumps({"compared_count": 0, "similarity_max": None}),
                "ACCEPT",
                "No terminated agents found — thesis accepted by default",
            ),
        )
        conn.commit()
        return (True, "")

    # Tokenize the new thesis
    new_tokens = _tokenize(thesis_text)
    if not new_tokens:
        conn.execute(
            "INSERT OR IGNORE INTO agents "
            "(id, name, status, spawn_date, config_json, current_thesis_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("__graveyard__", "__graveyard__", "system", now, "{}", 0),
        )
        conn.execute(
            "INSERT INTO evaluations (agent_id, evaluated_at, trades_evaluated, "
            "metrics_json, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "__graveyard__",
                now,
                0,
                json.dumps({"compared_count": len(rows), "similarity_max": 0.0}),
                "ACCEPT",
                "New thesis contains no tokens — accepted by default",
            ),
        )
        conn.commit()
        return (True, "")

    # Tokenize all terminated theses and build document-frequency map
    graveyard_entries: list[tuple[str, set[str]]] = []
    doc_freq: dict[str, int] = {}
    for row in rows:
        tokens = _tokenize(row["thesis_text"])
        unique = set(tokens)
        graveyard_entries.append((row["agent_name"], unique))
        for tok in unique:
            doc_freq[tok] = doc_freq.get(tok, 0) + 1

    if not graveyard_entries:
        conn.execute(
            "INSERT OR IGNORE INTO agents "
            "(id, name, status, spawn_date, config_json, current_thesis_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("__graveyard__", "__graveyard__", "system", now, "{}", 0),
        )
        conn.execute(
            "INSERT INTO evaluations (agent_id, evaluated_at, trades_evaluated, "
            "metrics_json, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "__graveyard__",
                now,
                0,
                json.dumps({"compared_count": len(rows), "similarity_max": 0.0}),
                "ACCEPT",
                "All terminated theses are empty — accepted by default",
            ),
        )
        conn.commit()
        return (True, "")

    # TF-IDF-like frequency filtering: words appearing in >80% of terminated
    # theses are too common to be discriminative.  Only meaningful with enough
    # documents to estimate document frequency.
    doc_count = len(graveyard_entries)
    stop_words: set[str] = set()
    if doc_count >= 3:
        max_df_ratio = 0.80
        stop_words = {
            word
            for word, freq in doc_freq.items()
            if freq / doc_count >= max_df_ratio
        }

    new_set = set(new_tokens) - stop_words

    # Compare against each terminated thesis
    max_similarity = 0.0
    most_similar_agent: str | None = None

    for agent_name, existing_tokens in graveyard_entries:
        existing_set = existing_tokens - stop_words

        if not new_set and not existing_set:
            similarity = 1.0
        elif not new_set or not existing_set:
            similarity = 0.0
        else:
            intersection = new_set & existing_set
            union = new_set | existing_set
            similarity = len(intersection) / len(union)

        if similarity > max_similarity:
            max_similarity = similarity
            most_similar_agent = agent_name

    if max_similarity > threshold:
        reason = (
            f"similar to terminated agent {most_similar_agent}: "
            f"Jaccard similarity {max_similarity:.3f} exceeds threshold {threshold}"
        )
    else:
        reason = (
            f"maximum Jaccard similarity {max_similarity:.3f} "
            f"is within threshold {threshold}"
        )

    decision = "REJECT" if max_similarity > threshold else "ACCEPT"

    conn.execute(
        "INSERT OR IGNORE INTO agents "
        "(id, name, status, spawn_date, config_json, current_thesis_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("__graveyard__", "__graveyard__", "system", now, "{}", 0),
    )
    conn.execute(
        "INSERT INTO evaluations (agent_id, evaluated_at, trades_evaluated, "
        "metrics_json, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "__graveyard__",
            now,
            0,
            json.dumps({
                "similarity_max": round(max_similarity, 4),
                "threshold": threshold,
                "most_similar_agent": most_similar_agent,
                "compared_count": len(graveyard_entries),
            }),
            decision,
            reason,
        ),
    )
    conn.commit()

    if max_similarity > threshold:
        return (False, reason)
    return (True, "")


def _serialise_config(overrides: dict) -> str:
    return json.dumps(overrides)
