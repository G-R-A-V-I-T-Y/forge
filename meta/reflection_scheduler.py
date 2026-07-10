"""meta/reflection_scheduler.py — Scheduled reflection triggers.

Reads the reflection trigger from the settings table and schedules
reflection cycles per-agent. Invokes agents/reflection.py's run_reflection
pipeline for eligible agents.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from agents.reflection import run_reflection
from store.db import get_agent

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_reflection_trigger(conn) -> dict[str, Any]:
    """Read the reflection trigger configuration from the settings table.

    Returns a dict with keys:
      - mode: "trade_count", "calendar_days", or "manual"
      - trade_interval: int (default 20)
      - day_interval: int (default 14)
    """
    import json

    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'reflection_trigger'"
    ).fetchone()
    if row:
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "mode": "trade_count",
        "trade_interval": 20,
        "day_interval": 14,
    }


def check_agent_eligible(conn, agent_id: str, trigger: dict[str, Any]) -> tuple[bool, str]:
    """Check whether an agent is due for a reflection cycle.

    Returns (True, "") if eligible, (False, reason) if not.
    """
    agent = get_agent(conn, agent_id)
    if agent is None:
        return False, "agent not found"

    if agent.get("status") in ("terminated", "culled"):
        return False, "agent is terminated"

    # Check if the agent has an active spec (compiled agents only)
    from store.specs import get_active_spec
    spec = get_active_spec(conn, agent_id)
    if spec is None:
        # Pure LLM agents without a spec can still reflect
        pass

    mode = trigger.get("mode", "trade_count")

    if mode == "manual":
        return False, "reflection trigger is set to manual"

    if mode == "trade_count":
        interval = trigger.get("trade_interval", 20)
        # Count trades since last reflection
        row = conn.execute(
            """SELECT COUNT(*) FROM trades
               WHERE agent_id = ? AND status = 'closed' AND voided = 0""",
            (agent_id,),
        ).fetchone()
        total_trades = row[0] if row else 0

        # Find the reflection trigger point: reflection fires when
        # total_trades crosses a multiple of interval since last reflection.
        last_row = conn.execute(
            """SELECT triggered_at FROM reflections
               WHERE agent_id = ? ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()

        if last_row is not None:
            trades_at_last = conn.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE agent_id = ? AND status = 'closed' AND voided = 0
                   AND entry_timestamp < ?""",
                (agent_id, last_row["triggered_at"]),
            ).fetchone()[0]
            trades_since = total_trades - trades_at_last
        else:
            trades_since = total_trades

        if trades_since < interval:
            return False, (
                f"only {trades_since} trades since last reflection"
                f" (need {interval})"
            )
        return True, ""

    if mode == "calendar_days":
        interval = trigger.get("day_interval", 14)
        last_row = conn.execute(
            """SELECT triggered_at FROM reflections
               WHERE agent_id = ? ORDER BY id DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()

        if last_row is None:
            return True, ""  # No previous reflection — allow

        try:
            last_ts = datetime.fromisoformat(
                last_row["triggered_at"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            return True, ""

        now = datetime.now(timezone.utc)
        days_since = (now - last_ts).days
        if days_since < interval:
            return False, (
                f"only {days_since} days since last reflection"
                f" (need {interval})"
            )
        return True, ""

    return False, f"unknown trigger mode: {mode}"


def run_reflection_cycle(
    conn,
    agent_id: str,
    config: dict,
    llm_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Run one reflection cycle for an agent, logging the result.

    Returns a dict with the ReflectionResult fields.
    """
    from agents.reflection import run_reflection as _run_reflection

    result = _run_reflection(conn, agent_id, config, llm_fn)

    # Log the outcome to the reflections table if it was triggered
    if result.triggered:
        rejection = None
        gate = None
        if not result.deployed:
            if result.blocked_by_gate:
                rejection = f"blocked by gate: {result.blocked_by_gate}"
                gate = result.blocked_by_gate
            elif result.rejection_reason:
                rejection = result.rejection_reason

        ev_summary = ""
        if result.gates_passed:
            ev_summary = f"Gates passed: {', '.join(result.gates_passed)}"
        if rejection:
            ev_summary += f" | Rejection: {rejection}"

        conn.execute(
            """UPDATE reflections SET outcome = ?, rejection_reason = ?
               WHERE agent_id = ? AND id = (
                   SELECT MAX(id) FROM reflections WHERE agent_id = ?
               )""",
            (
                "deployed" if result.deployed else "rejected",
                rejection,
                agent_id,
                agent_id,
            ),
        )
        conn.commit()

    return {
        "triggered": result.triggered,
        "deployed": result.deployed,
        "spec_version": result.spec_version,
        "blocked_by_gate": result.blocked_by_gate,
        "rejection_reason": result.rejection_reason,
        "gates_passed": result.gates_passed,
        "adversarial_flaws": result.adversarial_flaws,
    }
