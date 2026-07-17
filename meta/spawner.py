"""meta/spawner.py — Agent spawning, name generation, crossover spawning,
immigration quota, and desk diversity metrics.

M11.8-M11.10: Population operators and diversity.
"""
from __future__ import annotations

import json
import logging
import re
from itertools import combinations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_THESES_DIR = Path(__file__).parent.parent / "agents" / "theses"
_LEDGER_DIR = Path(__file__).parent.parent / "ledger"

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
    spawn_source: str = "fresh",
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
    spawn_source : str
        How this agent was spawned: 'fresh' (new thesis), 'seed' (crossover).

    Returns the agent row as a dict.
    """
    config_json = _serialise_config(config_overrides or {})
    now = _now()

    thesis_version = 1  # new agents always start at v1

    cursor = conn.execute(
        "INSERT OR IGNORE INTO agents (id, name, status, spawn_date, config_json, "
        "current_thesis_version, spawn_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, name, status, now, config_json, thesis_version, spawn_source),
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

    # M11.3 — Hypothesis graveyard: reject if the thesis targets a
    # (feature, direction) space already falsified by a prior agent.
    graveyard_reject = _check_hypothesis_graveyard(conn, new_tokens, config)
    if graveyard_reject is not None:
        return graveyard_reject

    return (True, "")


def _check_hypothesis_graveyard(
    conn, new_tokens: list[str], config: dict
) -> tuple[bool, str] | None:
    """Reject spawn if the thesis targets a falsified (feature, direction) space.

    Queries the hypotheses table for falsified entries and checks if the new
    thesis token set overlaps with any falsified claim's tokens AND mentions
    the same feature keyword.  Returns ``(False, reason)`` on rejection or
    ``None`` if no graveyard collision is found.
    """
    if not new_tokens:
        return None

    new_set = set(new_tokens)
    min_overlap = int(config.get("graveyard_claim_min_overlap", 2))

    falsified = conn.execute(
        """SELECT agent_id, claim, feature, direction, regime_context,
                  effect_observed
           FROM hypotheses
           WHERE status = 'falsified'"""
    ).fetchall()

    if not falsified:
        return None

    for row in falsified:
        claim_tokens = set(_tokenize(row["claim"] or ""))
        feature_token = (row["feature"] or "").lower()

        claim_overlap = len(claim_tokens & new_set)
        feature_in_thesis = feature_token and feature_token in new_set

        if claim_overlap >= min_overlap and feature_in_thesis:
            reason = (
                f"hypothesis graveyard: falsified by [{row['agent_id']}] "
                f"feature={row['feature']} dir={row['direction']} "
                f"regime={row['regime_context']} "
                f"(effect={row['effect_observed']:+.4f}): "
                f"{row['claim']}"
            )
            return (False, reason)

    return None


def _serialise_config(overrides: dict) -> str:
    return json.dumps(overrides)


# ======================================================================
# M11.8 — Crossover Spawning
# ======================================================================

_CROSSOVER_SYNTHESIS_SYSTEM_PROMPT = (
    "You are a quantitative trading strategist synthesizing a new trading thesis "
    "from the best trades of parent strategies. Combine the winning patterns, "
    "conditions, and market insights from the parent trades into a single, "
    "coherent thesis. The thesis should be novel — not a copy of any parent — "
    "but inherit the strongest elements from each. Output ONLY the thesis "
    "markdown, nothing else."
)

_SPEC_GENERATION_SYSTEM_PROMPT = (
    "You are a quantitative strategy engineer. Convert the following trading "
    "thesis into a structured spec YAML. The spec must follow this exact "
    "schema:\n"
    "  agent_id: __new__\n"
    "  spec_version: 1\n"
    "  thesis_version: 1\n"
    "  universe:\n"
    "    include: [list of assets]\n"
    "  regime_filter:\n"
    "    exclude: [list of regimes to skip]\n"
    "  entry:\n"
    "    direction: long | short | signal_determined\n"
    "    confidence_threshold: 0.6\n"
    "    scale_threshold: 0.8\n"
    "    evidence:\n"
    "      - name: descriptive_name\n"
    "        feature: feature_name\n"
    "        thresholds:\n"
    "          - op: '>' | '>=' | '<' | '<=' | 'between' | '==' | 'else'\n"
    "            weight: 0.5\n"
    "            value: number or [low, high] or null\n"
    "        missing: 'veto' | 'skip' | 'uncertainty:-0.1'\n"
    "    secondary_evidence: []\n"
    "  exit:\n"
    "    stop_loss_pct: 0.02\n"
    "    take_profit_pct: 0.04\n"
    "    max_hold_hours: 12\n"
    "  position:\n"
    "    leverage: 1\n"
    "    position_size_pct: 0.10\n\n"
    "Output ONLY the YAML, no explanation."
)


def spawn_from_seeds(
    conn,
    seed_ids: list[int],
    llm_fn: Callable[[str, str], str] | None,
    config: dict | None = None,
) -> str:
    """Synthesize a new agent from 2+ parent seeds via LLM crossover.

    Steps
    -----
    1. Query seeds table for the specified seed_ids (≥2 required).
    2. Load each seed's thesis_excerpt, key_conditions_met, pnl_pct.
    3. Build LLM prompt: "Synthesize one trading thesis from these parent
       strategies..."
    4. LLM generates new thesis markdown.
    5. Create spec YAML from thesis via reflection client.
    6. Run mandatory walk-forward on spec before first trade.
    7. If walk-forward passes, create agent with thesis+spec.
    8. Mark used seeds: UPDATE seeds SET used=1, spawned_agent_id=?

    Parameters
    ----------
    conn : sqlite3.Connection
        Active DB connection.
    seed_ids : list[int]
        At least 2 seed IDs to cross over.
    llm_fn : callable or None
        LLM completion function (system_prompt, user_prompt) -> str.
    config : dict or None
        Desk config; may contain walk-forward settings.

    Returns
    -------
    str
        The new agent_id.

    Raises
    ------
    ValueError
        If fewer than 2 seed_ids provided.
    RuntimeError
        If LLM is not available, spec parsing fails, or walk-forward rejects.
    """
    if len(seed_ids) < 2:
        raise ValueError("spawn_from_seeds requires at least 2 seed_ids")

    if config is None:
        config = {}

    # 1. Query seeds
    placeholders = ",".join("?" for _ in seed_ids)
    rows = conn.execute(
        f"SELECT id, agent_id, trade_id, pnl_pct, thesis_excerpt, key_conditions_met "
        f"FROM seeds WHERE id IN ({placeholders})",
        seed_ids,
    ).fetchall()

    if len(rows) < 2:
        raise ValueError(
            f"Expected ≥2 seeds, found {len(rows)} for ids={seed_ids}"
        )

    # Check none are already used
    for row in rows:
        used = conn.execute(
            "SELECT used FROM seeds WHERE id = ?", (row["id"],)
        ).fetchone()
        if used and used["used"]:
            raise ValueError(f"Seed {row['id']} already used")

    # 2. Build context for LLM
    parent_blocks = []
    for row in rows:
        block = (
            f"## Parent: {row['agent_id']} (seed #{row['id']})\n"
            f"- PnL: {row['pnl_pct']:.2%}\n"
            f"- Thesis excerpt: {row['thesis_excerpt'] or 'N/A'}\n"
            f"- Key conditions met: {row['key_conditions_met'] or 'N/A'}"
        )
        parent_blocks.append(block)

    parent_context = "\n\n".join(parent_blocks)

    # 3. LLM prompt
    user_prompt = (
        "Synthesize one trading thesis from these parent strategies. "
        "The new thesis should combine the strongest patterns and conditions "
        "from each parent into a novel, coherent approach.\n\n"
        f"{parent_context}\n\n"
        "Output a complete thesis in markdown format."
    )

    if llm_fn is None:
        raise RuntimeError("LLM function required for crossover spawn")

    # 4. Generate thesis
    thesis_text = llm_fn(_CROSSOVER_SYNTHESIS_SYSTEM_PROMPT, user_prompt)
    thesis_text = (thesis_text or "").strip()
    if not thesis_text:
        raise RuntimeError("LLM returned empty thesis for crossover spawn")

    # 5. Create spec YAML from thesis via reflection client
    spec_yaml = llm_fn(_SPEC_GENERATION_SYSTEM_PROMPT, f"Thesis:\n\n{thesis_text}")
    spec_yaml = (spec_yaml or "").strip()
    if not spec_yaml:
        raise RuntimeError("LLM returned empty spec for crossover spawn")

    from agents.reflection import parse_revised_spec  # noqa: PLC0415

    revised_spec = parse_revised_spec(spec_yaml, "__crossover__", 1)
    if revised_spec is None:
        raise RuntimeError("Failed to parse spec from crossover thesis")

    # 6. Run mandatory walk-forward
    try:
        from backtest.walk_forward import run_walk_forward  # noqa: PLC0415

        report = run_walk_forward(revised_spec, _LEDGER_DIR)
        if report.deflated_sharpe < 0.5:
            raise RuntimeError(
                f"Walk-forward rejected crossover spawn: "
                f"deflated_sharpe={report.deflated_sharpe:.3f} < 0.5"
            )
    except FileNotFoundError:
        logger.warning(
            "Walk-forward skipped: ledger data not found (first run?)"
        )

    # 7. Create agent
    name = generate_agent_name(conn)
    agent = spawn_agent(
        conn, name, thesis_text,
        status="rookie",
        spawn_source="seed",
    )
    agent_id = agent["id"]

    # Deploy the spec
    from store.specs import deploy_spec  # noqa: PLC0415

    deploy_spec(conn, agent_id, revised_spec, config)

    # 8. Mark seeds as used
    for row in rows:
        conn.execute(
            "UPDATE seeds SET used = 1, spawned_agent_id = ? WHERE id = ?",
            (agent_id, row["id"]),
        )
    conn.commit()

    logger.info(
        "Crossover spawn: agent=%s from seeds=%s",
        agent_id, seed_ids,
    )
    return agent_id


# ======================================================================
# M11.9 — Immigration Quota
# ======================================================================

_IMMIGRATION_WINDOW = 3  # look at last N spawns


def check_immigration_quota(conn) -> bool:
    """Check whether the last N spawns are all crossover (seed) spawns.

    If the last ``_IMMIGRATION_WINDOW`` spawns are ALL from seeds, immigration
    is required — the next spawn must be a fresh thesis to maintain diversity.

    Returns
    -------
    bool
        True if immigration required (all recent spawns are crossover).
    """
    rows = conn.execute(
        "SELECT spawn_source FROM agents "
        "WHERE id NOT LIKE 'benchmark_%' "
        "AND id != '__graveyard__' "
        "ORDER BY spawn_date DESC LIMIT ?",
        (_IMMIGRATION_WINDOW,),
    ).fetchall()

    if len(rows) < _IMMIGRATION_WINDOW:
        return False  # not enough history yet

    return all(
        (row["spawn_source"] or "fresh") == "seed" for row in rows
    )


# ======================================================================
# M11.10 — Diversity Metric
# ======================================================================

# Signal family inference: map feature name prefixes to high-level families.
_SIGNAL_FAMILY_MAP: dict[str, str] = {
    "return": "momentum",
    "momentum": "momentum",
    "rsi": "momentum",
    "ema": "trend",
    "vwap": "trend",
    "volume": "volume",
    "buy_volume": "volume",
    "sell_volume": "volume",
    "aggressor": "volume",
    "funding": "funding",
    "funding_zscore": "funding",
    "funding_acceleration": "funding",
    "open_interest": "order_flow",
    "oi_": "order_flow",
    "depth": "order_flow",
    "imbalance": "order_flow",
    "spread": "liquidity",
    "slippage": "liquidity",
    "atr": "volatility",
    "bb_": "volatility",
    "realized_vol": "volatility",
    "atr_percentile": "volatility",
    "liq_": "liquidation",
    "liquidation": "liquidation",
    "statistical_forecast": "forecast",
    "days_to_event": "event",
    "unlock_size": "event",
    "price": "price",
}

# SECTORS imported lazily to avoid circular import at module level.
_SECTORS: dict[str, list[str]] | None = None


def _load_sectors() -> dict[str, list[str]]:
    global _SECTORS  # noqa: PLW0603
    if _SECTORS is None:
        from market.heartbeat import SECTORS  # noqa: PLC0415
        _SECTORS = SECTORS
    return _SECTORS


def _infer_signal_family(feature_name: str) -> str:
    """Map a spec evidence feature name to a high-level signal family."""
    lower = feature_name.lower()
    for prefix, family in _SIGNAL_FAMILY_MAP.items():
        if lower.startswith(prefix):
            return family
    return "other"


def _extract_evidence_features(spec) -> set[str]:
    """Extract all feature names from a Spec's evidence terms."""
    features: set[str] = set()
    for term in spec.evidence:
        features.add(term.feature)
    for term in spec.secondary_evidence:
        features.add(term.feature)
    return features


def desk_diversity(conn) -> dict[str, Any]:
    """Compute desk-wide strategy diversity metrics.

    Steps
    -----
    1. Query all ACTIVE agents' specs.
    2. Extract evidence term features from each spec.
    3. Compute pairwise Jaccard overlap: |A ∩ B| / |A ∪ B|
    4. Compute per-(signal_family × sector) coverage from the SECTORS map.
    5. Return dict with: avg_jaccard, min_jaccard, coverage_by_family.

    Returns
    -------
    dict
        {
            "agent_count": int,
            "avg_jaccard": float,
            "min_jaccard": float,
            "coverage_by_family": {family: pct, ...},
            "family_sector_matrix": {"family|sector": True, ...},
        }
    """
    from backtest.dsl import load_spec  # noqa: PLC0415
    from store.specs import SPECS_DIR  # noqa: PLC0415

    # 1. Get active agents with specs
    agent_rows = conn.execute(
        """SELECT a.id, a.active_spec_version
           FROM agents a
           WHERE a.status IN ('active', 'rookie', 'shadow')
             AND a.id NOT LIKE 'benchmark_%'
             AND a.active_spec_version > 0"""
    ).fetchall()

    if len(agent_rows) < 2:
        return {
            "agent_count": len(agent_rows),
            "avg_jaccard": 0.0,
            "min_jaccard": 0.0,
            "coverage_by_family": {},
            "family_sector_matrix": {},
        }

    # Load specs and extract features
    agent_features: dict[str, set[str]] = {}
    agent_sectors: dict[str, set[str]] = {}

    sectors = _load_sectors()
    sector_asset_to_name: dict[str, str] = {}
    for sector_name, assets in sectors.items():
        for asset in assets:
            sector_asset_to_name[asset] = sector_name

    for row in agent_rows:
        aid = row["id"]
        spec_ver = row["active_spec_version"]
        filepath = SPECS_DIR / f"{aid}_v{spec_ver}.yaml"
        if not filepath.exists():
            continue
        try:
            spec = load_spec(str(filepath))
        except Exception:
            logger.warning("diversity: failed to load spec for %s", aid)
            continue

        agent_features[aid] = _extract_evidence_features(spec)
        agent_sectors[aid] = {
            sector_asset_to_name[a]
            for a in spec.universe_include
            if a in sector_asset_to_name
        }

    if len(agent_features) < 2:
        return {
            "agent_count": len(agent_features),
            "avg_jaccard": 0.0,
            "min_jaccard": 0.0,
            "coverage_by_family": {},
            "family_sector_matrix": {},
        }

    # 3. Pairwise Jaccard overlap on evidence features
    agent_ids = sorted(agent_features.keys())
    jaccard_values: list[float] = []

    for aid_a, aid_b in combinations(agent_ids, 2):
        features_a = agent_features[aid_a]
        features_b = agent_features[aid_b]
        if not features_a and not features_b:
            jaccard = 1.0
        elif not features_a or not features_b:
            jaccard = 0.0
        else:
            intersection = features_a & features_b
            union = features_a | features_b
            jaccard = len(intersection) / len(union)
        jaccard_values.append(jaccard)

    avg_jaccard = (
        sum(jaccard_values) / len(jaccard_values) if jaccard_values else 0.0
    )
    min_jaccard = min(jaccard_values) if jaccard_values else 0.0

    # 4. Per-(signal_family × sector) coverage
    all_families: set[str] = set()
    all_sectors: set[str] = set()
    family_sector_matrix: dict[tuple[str, str], bool] = {}

    for aid, features in agent_features.items():
        families = {_infer_signal_family(f) for f in features}
        all_families |= families
        sectors_covered = agent_sectors.get(aid, set())
        all_sectors |= sectors_covered

        for fam in families:
            for sec in sectors_covered:
                family_sector_matrix[(fam, sec)] = True

    # Compute coverage as fraction of (family × sector) cells occupied
    coverage_by_family: dict[str, float] = {}
    if all_families and all_sectors:
        for fam in sorted(all_families):
            occupied = sum(
                1 for sec in all_sectors
                if family_sector_matrix.get((fam, sec), False)
            )
            total = len(all_sectors)
            coverage_by_family[fam] = occupied / total if total > 0 else 0.0

    return {
        "agent_count": len(agent_features),
        "avg_jaccard": round(avg_jaccard, 4),
        "min_jaccard": round(min_jaccard, 4),
        "coverage_by_family": coverage_by_family,
        "family_sector_matrix": {
            f"{fam}|{sec}": True
            for (fam, sec) in family_sector_matrix
        },
    }


def get_least_covered_niche(conn) -> tuple[str, str]:
    """Find the (signal_family, sector) with lowest coverage.

    Returns a targeting hint for the next spawn — the system should prefer
    generating a thesis that covers this niche.

    Returns
    -------
    tuple[str, str]
        (signal_family, sector) with lowest coverage.
        Returns ("", "") if insufficient data.
    """
    diversity = desk_diversity(conn)
    matrix = diversity.get("family_sector_matrix", {})

    if not matrix:
        return ("", "")

    sectors = _load_sectors()
    all_sector_names = set(sectors.keys())
    all_families = set(
        fam_key.split("|")[0]
        for fam_key in matrix
    )

    if not all_families or not all_sector_names:
        return ("", "")

    # Find least covered: the cell present in the matrix with lowest true count
    # (the matrix only stores True entries, so "least covered" = not in matrix
    # means 0 agents cover it — we want the uncovered cell in the full grid)
    min_count = float("inf")
    least_covered = ("", "")

    for fam in sorted(all_families):
        for sec in sorted(all_sector_names):
            if (fam, sec) not in family_sector_matrix:
                # Completely uncovered — this is the least covered
                return (fam, sec)

    # All cells covered — find the least-populated
    for fam in sorted(all_families):
        for sec in sorted(all_sector_names):
            count = sum(
                1 for k in matrix
                if k == f"{fam}|{sec}"
            )
            if count < min_count:
                min_count = count
                least_covered = (fam, sec)

    return least_covered


# ======================================================================
# M11.8-11.10 — High-level spawn orchestrator
# ======================================================================

def decide_spawn_strategy(conn, config: dict | None = None) -> str:
    """Decide whether next spawn should be 'fresh' or 'seed' (crossover).

    Logic:
    - If immigration quota requires fresh → "fresh"
    - If least covered niche exists and seeds are available → "seed"
    - Otherwise → "fresh"

    Returns
    -------
    str
        "fresh" or "seed".
    """
    if config is None:
        config = {}

    # Immigration quota: force fresh if last N spawns are all crossover
    if check_immigration_quota(conn):
        return "fresh"

    # Check if there are unused seeds available
    unused_seeds = conn.execute(
        "SELECT COUNT(*) FROM seeds WHERE used = 0"
    ).fetchone()[0]

    if unused_seeds < 2:
        return "fresh"

    return "seed"
