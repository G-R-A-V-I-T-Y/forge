"""store/specs.py -- CRUD operations for the specs table and hot-reload detection.

The deploy pipeline is a pure data operation: it validates a Spec against the
desk config, writes the canonical YAML file, and records the deployment in
SQLite.  It has no coupling to the LLM infrastructure or the agent runtime.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.validator import validate_spec

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
                  deployed_at, rejection_reason, validation_errors
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
