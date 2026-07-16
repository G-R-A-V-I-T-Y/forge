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


#: Horizon policy (T8): both sides of the regret comparison are scored on a
#: single canonical horizon rather than averaged across whichever of
#: 1h/4h/24h happen to be labeled for a given decision. A per-decision
#: average across available horizons would let older decisions (more
#: horizons labeled) implicitly outweigh newer ones and would conflate
#: short-term noise (1h) with slow-to-resolve signal (24h) into one number.
#: 4h is the simplest defensible pick: long enough to smooth 5m-candle
#: noise, short enough that a 20-decision trial resolves in a reasonable
#: number of days. See meta/labeling.py::HORIZONS_HOURS.
RESOLUTION_HORIZON = "4h"


def resolve_challenger(
    conn: sqlite3.Connection, agent_id: str, horizon: str = RESOLUTION_HORIZON,
) -> dict:
    """Compare the challenger's shadow decisions against the incumbent and
    either **promote** or **reject** the challenger.

    The comparison uses *regret* — ``decision_labels.regret_pct`` (best-
    available outcome minus chosen outcome, computed nightly by
    ``meta/labeling.py::run_labeling_job``) at the canonical
    :data:`RESOLUTION_HORIZON`, averaged over the challenger's shadow
    decisions and the incumbent's executed decisions across the trial
    window. LOWER mean regret wins: the challenger is promoted when its
    mean regret is strictly lower than the incumbent's (status → ``active``,
    ``active_spec_version`` updated, incumbent demoted to ``inactive``).
    Otherwise the challenger is rejected (status → ``inactive``).

    When either side has **zero labeled decisions** in the trial window
    (the nightly labeling job hasn't caught up yet, or the incumbent simply
    hasn't traded), the trial is **not resolvable** — neither side's specs
    row is touched, so a caller can retry once more labels land rather than
    promoting or rejecting on zero evidence.

    Returns
    -------
    dict
        ``{"verdict": "promoted" | "rejected" | "not_resolvable" | "no_challenger",
           "challenger_version": int | None, "incumbent_version": int | None,
           "challenger_mean_regret": float | None,
           "incumbent_mean_regret": float | None,
           "challenger_labeled_decisions": int, "incumbent_labeled_decisions": int,
           "horizon": str}``. The ``"no_challenger"`` verdict returns just
        ``{"verdict": "no_challenger"}`` (no challenger deployed at all).
    """
    challenger = get_challenger_spec(conn, agent_id)
    if challenger is None:
        return {"verdict": "no_challenger"}

    incumbent = get_active_spec(conn, agent_id)
    incumbent_version = incumbent.spec_version if incumbent else 0

    # Trial window = the challenger's deployed_at forward. Both sides of the
    # comparison must be scoped to this window: an unbounded incumbent query
    # would average regret over the agent's ENTIRE decision history,
    # including decisions logged under prior spec versions that predate this
    # challenger trial entirely (T6 review finding 3).
    trial_row = conn.execute(
        """SELECT deployed_at FROM specs
           WHERE agent_id = ? AND status = 'challenger' AND spec_version = ?
           ORDER BY id DESC LIMIT 1""",
        (agent_id, challenger.spec_version),
    ).fetchone()
    trial_start = trial_row["deployed_at"] if trial_row else None

    challenger_regrets: list[float] = []
    incumbent_regrets: list[float] = []

    if trial_start is not None:
        # Join to decision_labels at the canonical horizon -- only LABELED
        # decisions ever enter the comparison. Production incumbent rows
        # (enter: {"order","fill"}; wait: {"candidate": {...}} or NULL;
        # close: {"position_id","fill"}) never carry "challenger_spec_version",
        # so the presence/absence of that key alone distinguishes the two
        # sides -- no confidence field required (the bug T8 replaces).
        rows = conn.execute(
            """SELECT d.decision_details_json, dl.regret_pct
               FROM decisions d
               JOIN decision_labels dl
                 ON dl.decision_id = d.id AND dl.horizon = ?
               WHERE d.agent_id = ? AND d.timestamp >= ?""",
            (horizon, agent_id, trial_start),
        ).fetchall()

        for row in rows:
            regret = row["regret_pct"]
            if regret is None:
                continue
            raw_details = row["decision_details_json"]
            try:
                details = json.loads(raw_details) if raw_details else {}
            except (json.JSONDecodeError, TypeError):
                details = {}

            if details.get("challenger_spec_version") == challenger.spec_version:
                challenger_regrets.append(regret)
            elif "challenger_spec_version" not in details:
                incumbent_regrets.append(regret)
            # Decisions carrying a *different* challenger's spec_version
            # (a prior, already-resolved trial) belong to neither side.

    challenger_count = len(challenger_regrets)
    incumbent_count = len(incumbent_regrets)

    if challenger_count == 0 or incumbent_count == 0:
        logger.info(
            "[%s] Challenger v%d not resolvable — %d challenger / %d incumbent"
            " labeled decisions in trial window (need >=1 each)",
            agent_id, challenger.spec_version, challenger_count, incumbent_count,
        )
        return {
            "verdict": "not_resolvable",
            "challenger_version": challenger.spec_version,
            "incumbent_version": incumbent_version,
            "challenger_mean_regret": None,
            "incumbent_mean_regret": None,
            "challenger_labeled_decisions": challenger_count,
            "incumbent_labeled_decisions": incumbent_count,
            "horizon": horizon,
        }

    challenger_mean = sum(challenger_regrets) / challenger_count
    incumbent_mean = sum(incumbent_regrets) / incumbent_count

    # Decision: promote when challenger has *strictly lower* mean regret.
    verdict = "promoted" if challenger_mean < incumbent_mean else "rejected"

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
            "[%s] Challenger v%d promoted (mean regret %.4f < incumbent %.4f)",
            agent_id, challenger.spec_version, challenger_mean, incumbent_mean,
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
            "[%s] Challenger v%d rejected (mean regret %.4f >= incumbent %.4f)",
            agent_id, challenger.spec_version, challenger_mean, incumbent_mean,
        )

    conn.commit()

    return {
        "verdict": verdict,
        "challenger_version": challenger.spec_version,
        "incumbent_version": incumbent_version,
        "challenger_mean_regret": round(challenger_mean, 4),
        "incumbent_mean_regret": round(incumbent_mean, 4),
        "challenger_labeled_decisions": challenger_count,
        "incumbent_labeled_decisions": incumbent_count,
        "horizon": horizon,
    }
