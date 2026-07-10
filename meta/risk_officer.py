"""meta/risk_officer.py — Central risk oversight for all agents.

Enforces per-agent and desk-wide risk limits:
  - Total drawdown kill switch (configurable % of desk equity)
  - Per-agent max position size
  - Daily loss limits
  - Suspicious activity monitoring (single-agent concentration)
  - Entry-gate disablement
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


class RiskOfficer:
    """Central risk supervisor. Loads desk config once per cycle check."""

    def __init__(self, conn, config: dict | None = None):
        self.conn = conn
        self.config = config or _load_config()

    # ── Desk-Level Checks ──────────────────────────────────────────

    def desk_in_kill_switch(self) -> bool:
        """Check whether total desk drawdown triggers the kill switch.

        Kill switch fires when the aggregate paper P&L drops below
        drawdown_kill_pct from peak equity.
        """
        kill_pct = float(self.config.get("drawdown_kill_pct", 25)) / 100.0

        total_balance = self.conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE mode = 'paper'"
        ).fetchone()[0]
        total_peak = self.conn.execute(
            "SELECT COALESCE(SUM(peak_balance), 0) FROM accounts WHERE mode = 'paper'"
        ).fetchone()[0]

        if total_peak <= 0:
            return False

        drawdown = (total_peak - total_balance) / total_peak
        if drawdown >= kill_pct:
            logger.warning(
                "DESK KILL SWITCH: drawdown %.1f%% >= %.1f%%",
                drawdown * 100, kill_pct * 100,
            )
            return True
        return False

    def desk_daily_loss_exceeded(self) -> bool:
        """Check if total desk daily loss exceeds the configured limit."""
        daily_loss_limit = float(self.config.get("daily_loss_limit", 500))

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = self.conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) FROM trades
               WHERE status = 'closed' AND voided = 0
               AND DATE(entry_timestamp) = ?""",
            (today,),
        ).fetchone()[0]

        if daily_pnl < -daily_loss_limit:
            logger.warning(
                "DESK DAILY LOSS: %.2f exceeds limit %.2f",
                daily_pnl, -daily_loss_limit,
            )
            return True
        return False

    def agent_concentration_exceeded(self, threshold: float = 0.40) -> list[str]:
        total_positions = self.conn.execute(
            "SELECT COALESCE(SUM(notional_usd), 0) FROM positions"
        ).fetchone()[0]

        if total_positions <= 0:
            return []

        rows = self.conn.execute(
            """SELECT agent_id, COALESCE(SUM(notional_usd), 0) as total
               FROM positions GROUP BY agent_id"""
        ).fetchall()

        violators = []
        for row in rows:
            share = row["total"] / total_positions if total_positions > 0 else 0
            if share > threshold:
                violators.append(row["agent_id"])
        return violators

    # ── Per-Agent Checks ──────────────────────────────────────────

    def agent_position_limit_exceeded(self, agent_id: str) -> bool:
        max_pos = float(self.config.get("max_position_size", 1000))
        current_size = self.conn.execute(
            """SELECT COALESCE(SUM(notional_usd), 0) FROM positions
               WHERE agent_id = ?""",
            (agent_id,),
        ).fetchone()[0]
        return current_size > max_pos

    def agent_daily_loss_exceeded(self, agent_id: str) -> bool:
        """Check if agent exceeded its daily loss limit."""
        daily_limit = float(self.config.get("agent_daily_loss", 100))

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = self.conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) FROM trades
               WHERE agent_id = ? AND status = 'closed' AND voided = 0
               AND DATE(entry_timestamp) = ?""",
            (agent_id, today),
        ).fetchone()[0]

        return pnl < -daily_limit

    # ── Entry-Gate Management ─────────────────────────────────────

    def is_entry_gate_open(self, agent_id: str) -> bool:
        """Check if the entry gate is open for this agent.

        An entry can be disabled (gate closed) by:
          - Desk kill switch active
          - Agent-specific disable (via entry_disables table)
          - Individual risk rule violation
        """
        if self.desk_in_kill_switch():
            return False

        disabled = self.conn.execute(
            """SELECT 1 FROM entry_disables
               WHERE agent_id = ? AND enabled_at IS NULL
               LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if disabled:
            return False

        if self.agent_position_limit_exceeded(agent_id):
            return False

        if self.agent_daily_loss_exceeded(agent_id):
            return False

        return True

    def disable_entry(self, agent_id: str, reason: str) -> None:
        """Disable the entry gate for an agent."""
        self.conn.execute(
            """INSERT INTO entry_disables
                   (agent_id, disabled_at, reason)
               VALUES (?, ?, ?)""",
            (agent_id, _now(), reason),
        )
        self.conn.commit()
        logger.info("Entry disabled for %s: %s", agent_id, reason)

    def enable_entry(self, agent_id: str) -> None:
        """Re-enable the entry gate for an agent."""
        self.conn.execute(
            "UPDATE entry_disables SET enabled_at = ? WHERE agent_id = ? AND enabled_at IS NULL",
            (_now(), agent_id),
        )
        self.conn.commit()
        logger.info("Entry re-enabled for %s", agent_id)

    # ── Full Risk Check ──────────────────────────────────────────

    def run_cycle(self) -> dict[str, Any]:
        """Run a full risk-check cycle across all agents.

        Returns a report dict with desk and per-agent findings.
        """
        report = {
            "checked_at": _now(),
            "desk_kill_switch": self.desk_in_kill_switch(),
            "desk_daily_loss_exceeded": self.desk_daily_loss_exceeded(),
            "concentration_violators": self.agent_concentration_exceeded(),
            "agents": {},
        }

        # Check each active/rookie agent
        rows = self.conn.execute(
            """SELECT id, status FROM agents
               WHERE status IN ('rookie', 'active', 'suspended')
               ORDER BY name"""
        ).fetchall()

        for row in rows:
            agent_id = row["id"]
            status = row["status"]
            agent_report = {
                "status": status,
                "position_limit_exceeded": self.agent_position_limit_exceeded(agent_id),
                "daily_loss_exceeded": self.agent_daily_loss_exceeded(agent_id),
                "entry_gate_open": self.is_entry_gate_open(agent_id),
            }

            if not agent_report["entry_gate_open"] and status not in ("suspended", "terminated"):
                self.disable_entry(agent_id, "Entry blocked by risk check")

            report["agents"][agent_id] = agent_report

        return report


def risk_check_cycle(conn, config: dict | None = None) -> dict[str, Any]:
    """Convenience function: instantiate RiskOfficer and run one cycle."""
    officer = RiskOfficer(conn, config)
    return officer.run_cycle()


def apply_risk_verdict(
    result: dict[str, Any],
) -> bool:
    """Return True if the desk is cleared for trading based on a risk cycle result."""
    if result.get("desk_kill_switch"):
        return False
    if result.get("desk_daily_loss_exceeded"):
        return False
    return True
