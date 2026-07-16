"""store/specs.py -- CRUD operations for the specs table and hot-reload detection.

The deploy pipeline is a pure data operation: it validates a Spec against the
desk config, writes the canonical YAML file, and records the deployment in
SQLite.  It has no coupling to the LLM infrastructure or the agent runtime.

M10 challenger trial: specs can also be deployed as ``challenger`` — shadow
evaluated alongside the incumbent without executing trades.  Once enough data
has accumulated, :func:`resolve_challenger` compares regret and either
promotes or rejects the challenger.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.validator import validate_spec

logger = logging.getLogger(__name__)

#: Directory where versioned spec YAML files live.
SPECS_DIR = Path(__file__).resolve().parent.parent / "agents" / "specs"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _spec_to_dict(spec: Spec) -> dict:
    """Serialize a frozen Spec dataclass back to the canonical YAML dict shape."""
    def _threshold_to_dict(t: Threshold) -> dict:
        d: dict = {"op": t.op, "weight": t.weight}
        if t.value is not None:
            d["value"] = t.value
        return d

    def _evidence_to_dict(terms: list[EvidenceTerm]) -> list[dict]:
        return [
            {
                "name": t.name,
                "feature": t.feature,
                "thresholds": [_threshold_to_dict(th) for th in t.thresholds],
                "missing": t.missing,
            }
            for t in terms
        ]

    return {
        "agent_id": spec.agent_id,
        "spec_version": spec.spec_version,
        "thesis_version": spec.thesis_version,
        "universe": {"include": list(spec.universe_include)},
        "regime_filter": {"exclude": list(spec.regime_exclude)},
        "entry": {
            "direction": spec.direction,
            "confidence_threshold": spec.confidence_threshold,
            "scale_threshold": spec.scale_threshold,
            "evidence": _evidence_to_dict(spec.evidence),
            "secondary_evidence": _evidence_to_dict(spec.secondary_evidence),
        },
        "exit": {
            "stop_loss_pct": spec.stop_loss_pct,
            "take_profit_pct": spec.take_profit_pct,
            "max_hold_hours": spec.max_hold_hours,
        },
        "position": {
            "leverage": spec.leverage,
            "position_size_pct": spec.position_size_pct,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deploy_spec(
    conn: sqlite3.Connection,
    agent_id: str,
    spec: Spec,
    config: dict | None = None,
) -> int:
    """Deploy a *Spec* to the active slot for *agent_id*.

    Steps
    -----
    1. Validate the spec against the desk *config* (if provided) via
       ``backtest/validator.validate_spec``.
    2. Serialise the spec to YAML and write it to
       ``agents/specs/{agent_id}_v{spec_version}.yaml``.
    3. Mark any previously-active spec for this agent as ``inactive``.
    4. Insert a new row in the ``specs`` table — status is ``active`` when
       validation passes, ``rejected`` otherwise, with details captured in
       ``rejection_reason`` and ``validation_errors``.
    5. Update the agent row's ``active_spec_version`` to the deployed version.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier (matches ``agents.id``).
    spec:
        The spec to deploy.
    config:
        Desk configuration dict (``max_leverage``, ``max_position_size_pct``).
        When ``None`` validation is skipped so callers that hold a pre-validated
        spec can avoid passing config.

    Returns
    -------
    int
        The ``specs.id`` of the newly-inserted row.
    """
    # 1. Validate
    errors: list[str] = []
    if config is not None:
        errors = validate_spec(spec, config)

    validation_errors: str | None = "; ".join(errors) if errors else None
    rejection_reason: str | None = None
    status: str = "active"

    if errors:
        status = "rejected"
        rejection_reason = "spec rejected by deploy pipeline — see validation_errors"

    # 2. Write YAML file (skip if byte-identical to avoid unnecessary
    #    file-touching on startup reconciliation).
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = SPECS_DIR / f"{agent_id}_v{spec.spec_version}.yaml"
    spec_dict = _spec_to_dict(spec)
    yaml_text = yaml.dump(
        spec_dict,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if not (filepath.exists() and filepath.read_bytes() == yaml_text.encode("utf-8")):
        filepath.write_text(yaml_text, encoding="utf-8")

    # 3–5. DB transaction
    conn.execute(
        "UPDATE specs SET status = 'inactive' WHERE agent_id = ? AND status = 'active'",
        (agent_id,),
    )
    cursor = conn.execute(
        """INSERT INTO specs
               (agent_id, spec_version, thesis_version, yaml_text,
                status, deployed_at, rejection_reason, validation_errors)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            agent_id,
            spec.spec_version,
            spec.thesis_version,
            yaml_text,
            status,
            _now(),
            rejection_reason,
            validation_errors,
        ),
    )
    if status == "active":
        conn.execute(
            "UPDATE agents SET active_spec_version = ? WHERE id = ?",
            (spec.spec_version, agent_id),
        )
    conn.commit()

    return cursor.lastrowid


def get_active_spec(conn: sqlite3.Connection, agent_id: str) -> Spec | None:
    """Return the currently-active :class:`Spec` for *agent_id*, or ``None``.

    The spec is reconstructed from the canonical YAML file on disk so that it
    always reflects the exact byte content the backtester / agent would see.
    """
    row = conn.execute(
        """SELECT spec_version FROM specs
           WHERE agent_id = ? AND status = 'active'
           ORDER BY spec_version DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()

    if row is None:
        return None

    filepath = SPECS_DIR / f"{agent_id}_v{row['spec_version']}.yaml"
    if not filepath.exists():
        return None

    from backtest.dsl import load_spec  # noqa: PLC0415 – lazy import avoids cycle

    return load_spec(str(filepath))


def get_spec_history(conn: sqlite3.Connection, agent_id: str) -> list[dict]:
    """Return every spec version deployed for *agent_id*, newest first."""
    rows = conn.execute(
        """SELECT id, spec_version, thesis_version, status,
                  deployed_at, rejection_reason, validation_errors, yaml_text
           FROM specs
           WHERE agent_id = ?
           ORDER BY spec_version DESC""",
        (agent_id,),
    ).fetchall()

    return [dict(r) for r in rows]


def hot_reload_spec(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Check whether *agent_id* has a newer spec available for reload.

    Compares the maximum ``spec_version`` among active specs in the DB against
    the agent row's ``active_spec_version`` column.  Returns ``True`` when the
    DB has a newer version that the agent should reload.

    The caller (typically the agent runtime) is responsible for calling
    :func:`get_active_spec` to obtain the new spec and then updating
    ``agents.active_spec_version`` to the new version once loaded.
    """
    agent_row = conn.execute(
        "SELECT active_spec_version FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if agent_row is None:
        return False

    current_version: int = agent_row["active_spec_version"] or 0

    max_row = conn.execute(
        """SELECT MAX(spec_version) AS max_version FROM specs
           WHERE agent_id = ? AND status = 'active'""",
        (agent_id,),
    ).fetchone()
    if max_row is None or max_row["max_version"] is None:
        return False

    return max_row["max_version"] > current_version


# ---------------------------------------------------------------------------
# M10: Challenger trial
# ---------------------------------------------------------------------------


def get_spec_by_status(
    conn: sqlite3.Connection, agent_id: str, status: str,
) -> Spec | None:
    """Return the latest :class:`Spec` for *agent_id* with the given *status*.

    The spec is reconstructed from the canonical YAML file on disk so that it
    always reflects the exact byte content the backtester / agent would see.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier (matches ``agents.id``).
    status:
        Spec status to filter by (e.g. ``'active'``, ``'challenger'``).

    Returns
    -------
    Spec | None
        The most recent spec with the requested status, or ``None``.
    """
    row = conn.execute(
        """SELECT spec_version FROM specs
           WHERE agent_id = ? AND status = ?
           ORDER BY spec_version DESC LIMIT 1""",
        (agent_id, status),
    ).fetchone()

    if row is None:
        return None

    filepath = SPECS_DIR / f"{agent_id}_v{row['spec_version']}.yaml"
    if not filepath.exists():
        return None

    from backtest.dsl import load_spec  # noqa: PLC0415 – lazy import avoids cycle

    return load_spec(str(filepath))


def deploy_as_challenger(
    conn: sqlite3.Connection,
    agent_id: str,
    spec: Spec,
    config: dict | None = None,
) -> int:
    """Deploy a *Spec* as a **challenger** for *agent_id*.

    A challenger spec is shadow-evaluated alongside the active incumbent:
    each heartbeat the compiled body evaluates *both* specs, but only the
    incumbent's decision is acted on.  The challenger's decisions are logged
    for later regret comparison.

    Unlike :func:`deploy_spec`, this does **not**:

    - Mark any existing active spec as inactive.
    - Update the agent's ``active_spec_version``.

    Steps
    -----
    1. Validate the spec against the desk *config* (if provided).
    2. Write the canonical YAML file.
    3. Insert a row in ``specs`` with ``status = 'challenger'``.
    4. Reject any previous challenger for this agent (mark as ``inactive``).

    Parameters
    ----------
    conn:
        Open SQLite connection.
    agent_id:
        Agent identifier.
    spec:
        The challenger spec.
    config:
        Desk configuration dict for validation.  ``None`` skips validation.

    Returns
    -------
    int
        The ``specs.id`` of the newly-inserted challenger row.
    """
    # 1. Validate
    errors: list[str] = []
    if config is not None:
        errors = validate_spec(spec, config)

    validation_errors: str | None = "; ".join(errors) if errors else None
    rejection_reason: str | None = None
    status: str = "challenger"

    if errors:
        status = "rejected"
        rejection_reason = "challenger rejected by deploy pipeline — see validation_errors"

    # 2. Write YAML file
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = SPECS_DIR / f"{agent_id}_v{spec.spec_version}.yaml"
    spec_dict = _spec_to_dict(spec)
    yaml_text = yaml.dump(
        spec_dict,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if not (filepath.exists() and filepath.read_bytes() == yaml_text.encode("utf-8")):
        filepath.write_text(yaml_text, encoding="utf-8")

    # 3–4. DB transaction
    # Demote any previous challenger so only one is active at a time.
    conn.execute(
        "UPDATE specs SET status = 'inactive' WHERE agent_id = ? AND status = 'challenger'",
        (agent_id,),
    )
    cursor = conn.execute(
        """INSERT INTO specs
               (agent_id, spec_version, thesis_version, yaml_text,
                status, deployed_at, rejection_reason, validation_errors)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            agent_id,
            spec.spec_version,
            spec.thesis_version,
            yaml_text,
            status,
            _now(),
            rejection_reason,
            validation_errors,
        ),
    )
    conn.commit()

    return cursor.lastrowid


def get_challenger_spec(conn: sqlite3.Connection, agent_id: str) -> Spec | None:
    """Return the currently-active challenger :class:`Spec` for *agent_id*.

    At most one challenger can be active per agent at any time.  Returns
    ``None`` when no challenger has been deployed (or after resolution).
    """
    return get_spec_by_status(conn, agent_id, "challenger")


def resolve_challenger(conn: sqlite3.Connection, agent_id: str) -> dict:
    """Compare the challenger's shadow decisions against the incumbent and
    either **promote** or **reject** the challenger.

    The comparison uses *regret* — the difference between the challenger's
    average logged confidence and the incumbent's average logged confidence
    over the trial period.  When the challenger's average confidence exceeds
    the incumbent's, the challenger is promoted (status → ``active``,
    ``active_spec_version`` updated, incumbent demoted to ``inactive``).
    Otherwise the challenger is rejected (status → ``inactive``).

    Returns
    -------
    dict
        ``{"verdict": "promoted" | "rejected" | "no_challenger",
           "challenger_version": int | None, "incumbent_version": int | None,
           "challenger_avg_confidence": float | None,
           "incumbent_avg_confidence": float | None,
           "challenger_decisions": int, "incumbent_decisions": int}``.
    """
    challenger = get_challenger_spec(conn, agent_id)
    if challenger is None:
        return {"verdict": "no_challenger"}

    incumbent = get_active_spec(conn, agent_id)
    incumbent_version = incumbent.spec_version if incumbent else 0

    # Gather challenger trial decisions (logged via decision_loop with
    # challenger_spec_version in decision_details_json).
    challenger_rows = conn.execute(
        """SELECT decision_details_json FROM decisions
           WHERE agent_id = ? AND decision_details_json IS NOT NULL
           ORDER BY timestamp ASC""",
        (agent_id,),
    ).fetchall()

    challenger_conf_sum = 0.0
    challenger_count = 0
    incumbent_conf_sum = 0.0
    incumbent_count = 0

    for row in challenger_rows:
        try:
            details = json.loads(row["decision_details_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        # Challenger-logged decisions carry "challenger_spec_version".
        if details.get("challenger_spec_version") == challenger.spec_version:
            conf = details.get("challenger_confidence")
            if isinstance(conf, (int, float)):
                challenger_conf_sum += conf
                challenger_count += 1

        # Incumbent decisions on the same period (for baseline comparison).
        if details.get("challenger_spec_version") is None:
            conf = details.get("confidence") or details.get("challenger_confidence")
            if isinstance(conf, (int, float)):
                incumbent_conf_sum += conf
                incumbent_count += 1

    challenger_avg = challenger_conf_sum / challenger_count if challenger_count else 0.0
    incumbent_avg = incumbent_conf_sum / incumbent_count if incumbent_count else 0.0

    # Decision: promote when challenger has *strictly* higher average
    # confidence AND at least one logged decision.
    verdict = "rejected"
    if challenger_count > 0 and challenger_avg > incumbent_avg:
        verdict = "promoted"

    if verdict == "promoted":
        # Demote the current incumbent.
        conn.execute(
            """UPDATE specs SET status = 'inactive'
               WHERE agent_id = ? AND status = 'active'""",
            (agent_id,),
        )
        # Promote the challenger.
        conn.execute(
            """UPDATE specs SET status = 'active'
               WHERE id = (
                   SELECT id FROM specs
                   WHERE agent_id = ? AND status = 'challenger'
                   AND spec_version = ?
                   ORDER BY id DESC LIMIT 1
               )""",
            (agent_id, challenger.spec_version),
        )
        conn.execute(
            "UPDATE agents SET active_spec_version = ? WHERE id = ?",
            (challenger.spec_version, agent_id),
        )
        logger.info(
            "[%s] Challenger v%d promoted (avg_conf %.3f > incumbent %.3f)",
            agent_id, challenger.spec_version, challenger_avg, incumbent_avg,
        )
    else:
        # Reject the challenger.
        conn.execute(
            """UPDATE specs SET status = 'inactive'
               WHERE agent_id = ? AND status = 'challenger'
               AND spec_version = ?""",
            (agent_id, challenger.spec_version),
        )
        logger.info(
            "[%s] Challenger v%d rejected (avg_conf %.3f <= incumbent %.3f)",
            agent_id, challenger.spec_version, challenger_avg, incumbent_avg,
        )

    conn.commit()

    return {
        "verdict": verdict,
        "challenger_version": challenger.spec_version,
        "incumbent_version": incumbent_version,
        "challenger_avg_confidence": round(challenger_avg, 4),
        "incumbent_avg_confidence": round(incumbent_avg, 4),
        "challenger_decisions": challenger_count,
        "incumbent_decisions": incumbent_count,
    }
