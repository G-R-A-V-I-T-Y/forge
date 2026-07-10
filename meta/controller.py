"""meta/controller.py — Meta-controller evaluation loop.

Runs on a per-agent trade-count cadence: assesses each agent against the
null distribution, enforces lifecycle rules (suspend/terminate/harvest),
and logs evaluation results.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from store.db import get_agent
from store.performance import compute_metrics
from meta.evaluator import (
    get_null_metrics,
    get_lifecycle_decision,
    harvest_best_trades,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_evaluation_interval(conn) -> int:
    """Read the evaluation interval (in trades) from the settings table."""
    import json

    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'evaluation_interval'"
    ).fetchone()
    if row:
        try:
            return int(json.loads(row["value"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return 30  # default: every 30 trades


def get_evaluation_thresholds(conn) -> dict[str, Any]:
    """Read evaluation thresholds from the settings table."""
    import json

    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'evaluation_thresholds'"
    ).fetchone()
    if row:
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "win_rate_terminate": 0.35,
        "drawdown_suspend": 0.20,
        "pf_suspend": 0.8,
        "min_trades_significance": 30,
        "min_trades_terminate": 50,
        "min_trades_promotion": 100,
        "probation_days": 7,
        "probation_trades": 10,
    }


def evaluate_agent(
    conn,
    agent_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Evaluate one agent against the lifecycle rules.

    Returns the evaluation result dict.
    """
    agent = get_agent(conn, agent_id)
    if agent is None:
        return {"error": f"Agent {agent_id} not found"}

    # Skip terminated/culled agents
    if agent.get("status") in ("terminated", "culled"):
        return {"skipped": True, "reason": "agent already terminated"}

    metrics = compute_metrics(conn, agent_id)
    closed_trades = metrics.get("closed_trades", 0)

    # Check if evaluation is due
    if not force:
        interval = get_evaluation_interval(conn)
        last_eval = conn.execute(
            """SELECT evaluated_at FROM evaluations
               WHERE agent_id = ? ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()

        if last_eval:
            trades_at_last = conn.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE agent_id = ? AND status = 'closed' AND voided = 0
                   AND entry_timestamp < ?""",
                (agent_id, last_eval["evaluated_at"]),
            ).fetchone()[0]
            trades_since = closed_trades - trades_at_last
        else:
            trades_since = closed_trades

        if trades_since < interval and closed_trades > 0:
            return {"skipped": True, "reason": f"only {trades_since} trades since last eval (need {interval})"}

    # Get null metrics for significance testing
    null_metrics = get_null_metrics(conn)

    # Get lifecycle decision
    lifecycle = get_lifecycle_decision(conn, agent_id, metrics, null_metrics)
    decision = lifecycle["decision"]
    reason = lifecycle["reason"]
    trigger = lifecycle["trigger"]

    # Store evaluation
    current_status = agent["status"]
    new_status = current_status

    if decision == "suspend":
        new_status = "suspended"
    elif decision == "terminate":
        new_status = "terminated"
    elif decision == "active" and current_status == "suspended":
        new_status = "active"

    metrics_json = json.dumps({
        "win_rate": metrics.get("win_rate", 0),
        "profit_factor": metrics.get("profit_factor", 0),
        "sharpe": metrics.get("sharpe", 0),
        "closed_trades": metrics.get("closed_trades", 0),
        "last_7d_return": metrics.get("last_7d_return", 0),
        "max_drawdown": lifecycle.get("max_drawdown", 0),
        "trigger": trigger,
    })

    now = _now()
    conn.execute(
        """INSERT INTO evaluations
               (agent_id, evaluated_at, trades_evaluated, metrics_json, decision, reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_id, now, closed_trades, metrics_json, decision, reason),
    )
    conn.commit()

    # Update agent status if changed
    if new_status != current_status:
        old_status = current_status
        conn.execute(
            "UPDATE agents SET status = ? WHERE id = ?",
            (new_status, agent_id),
        )
        conn.commit()
        logger.info(
            "Agent %s: %s → %s (%s: %s)",
            agent_id, old_status, new_status, trigger, reason,
        )

    # If terminated, harvest best trades
    harvested = []
    if decision == "terminate":
        harvested = harvest_best_trades(conn, agent_id, count=5)
        logger.info(
            "Agent %s terminated — harvested %d best trades",
            agent_id, len(harvested),
        )

    return {
        "agent_id": agent_id,
        "decision": decision,
        "reason": reason,
        "trigger": trigger,
        "old_status": current_status,
        "new_status": new_status,
        "harvested": len(harvested),
        "closed_trades": closed_trades,
    }


def run_evaluation_cycle(conn) -> list[dict[str, Any]]:
    """Evaluate all active/rookie agents.

    Returns a list of evaluation result dicts.
    """
    rows = conn.execute(
        "SELECT id FROM agents WHERE status IN ('rookie', 'active', 'suspended') ORDER BY name"
    ).fetchall()

    results = []
    for row in rows:
        try:
            result = evaluate_agent(conn, row["id"])
            results.append(result)
        except Exception as exc:
            logger.error("Failed to evaluate agent %s: %s", row["id"], exc, exc_info=True)
            results.append({"agent_id": row["id"], "error": str(exc)})

    return results
