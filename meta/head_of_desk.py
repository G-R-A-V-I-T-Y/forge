"""meta/head_of_desk.py — Agent diversity and population management."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import yaml

from meta.spawner import spawn_agent

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


def get_agent_roster(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.id, a.name, a.status, a.config_json,
                  a.spawn_date,
                  COALESCE(SUM(t.pnl_usd), 0) AS total_pnl,
                  COUNT(t.id) AS closed_trades
           FROM agents a
           LEFT JOIN trades t ON t.agent_id = a.id AND t.status = 'closed' AND t.voided = 0
           GROUP BY a.id
           ORDER BY a.name"""
    ).fetchall()

    roster = []
    for row in rows:
        roster.append({
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "config_json": row["config_json"],
            "spawn_date": row["spawn_date"],
            "total_pnl": float(row["total_pnl"]),
            "closed_trades": int(row["closed_trades"]),
        })
    return roster


def get_strategy_distribution(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT config_json FROM agents WHERE status NOT IN ('terminated', 'culled')"
    ).fetchall()

    distribution: dict[str, int] = {}
    for row in rows:
        strategy = "unknown"
        if row["config_json"]:
            try:
                pc = json.loads(row["config_json"])
                strategy = pc.get("strategy", pc.get("persona", "unknown"))
            except (json.JSONDecodeError, TypeError):
                strategy = "unknown"

        distribution[strategy] = distribution.get(strategy, 0) + 1
    return distribution


def _seed_thesis_for_archetype(archetype: dict) -> str:
    return (
        f"Seed thesis: {archetype['persona']} strategy.\n"
        f"Strategy: {archetype['strategy']}\n"
        f"Risk tolerance: {archetype['risk_tolerance']}\n"
    )


def ensure_agent_count(conn, config: dict | None = None) -> list[str]:
    if config is None:
        config = _load_config()

    target = int(config.get("target_agent_count", 5))
    max_agents = int(config.get("max_agents", 20))

    current_count = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE status IN ('rookie', 'active')"
    ).fetchone()[0]

    if current_count >= target:
        return []

    deficit = min(target - current_count, max_agents - current_count)
    if deficit <= 0:
        return []

    logger.info("Agent count %d below target %d -- spawning %d", current_count, target, deficit)

    archetypes = [
        {"strategy": "momentum", "persona": "Momentum Trader", "risk_tolerance": "aggressive"},
        {"strategy": "mean_reversion", "persona": "Mean Reversion Trader", "risk_tolerance": "moderate"},
        {"strategy": "trend_following", "persona": "Trend Follower", "risk_tolerance": "conservative"},
        {"strategy": "breakout", "persona": "Breakout Trader", "risk_tolerance": "aggressive"},
        {"strategy": "scalping", "persona": "Scalper", "risk_tolerance": "moderate"},
    ]

    spawned = []
    for i in range(deficit):
        archetype = archetypes[i % len(archetypes)]
        config_overrides = {
            "strategy": archetype["strategy"],
            "persona": archetype["persona"],
            "risk_tolerance": archetype["risk_tolerance"],
            "spawned_by": "head_of_desk",
        }
        name = f"agent_{archetype['strategy']}_{len(spawned) + 1}"
        thesis = _seed_thesis_for_archetype(archetype)

        try:
            result = spawn_agent(
                conn,
                name=name,
                seed_thesis_text=thesis,
                status="rookie",
                config_overrides=config_overrides,
            )
            spawned.append(result["id"])
            logger.info("Spawned %s (%s archetype)", name, archetype["strategy"])
        except Exception as exc:
            logger.error("Failed to spawn %s archetype: %s", archetype["strategy"], exc)

    return spawned


def cull_if_overpopulated(conn, config: dict | None = None) -> list[str]:
    if config is None:
        config = _load_config()

    max_agents = int(config.get("max_agents", 20))
    current_count = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE status IN ('rookie', 'active', 'suspended')"
    ).fetchone()[0]

    if current_count <= max_agents:
        return []

    excess = current_count - max_agents
    logger.info("Agent count %d exceeds max %d -- culling %d", current_count, max_agents, excess)

    candidates = conn.execute(
        """SELECT a.id, COALESCE(SUM(t.pnl_usd), 0) AS total_pnl, COUNT(t.id) AS trade_count
           FROM agents a
           LEFT JOIN trades t ON t.agent_id = a.id AND t.status = 'closed' AND t.voided = 0
           WHERE a.status IN ('rookie', 'active', 'suspended')
           GROUP BY a.id
           HAVING trade_count >= 10
           ORDER BY total_pnl ASC
           LIMIT ?""",
        (excess,),
    ).fetchall()

    culled = []
    for row in candidates:
        agent_id = row["id"]
        now = _now()
        conn.execute(
            """INSERT INTO audit_log
                   (agent_id, action, details_json, created_at)
               VALUES (?, 'culled', ?, ?)""",
            (agent_id, json.dumps({"reason": "overpopulation cull", "total_pnl": float(row["total_pnl"])}), now),
        )
        conn.execute("UPDATE agents SET status = 'culled' WHERE id = ?", (agent_id,))
        culled.append(agent_id)
        logger.info("Culled %s (total_pnl=%.2f, trades=%d)", agent_id, row["total_pnl"], row["trade_count"])

    conn.commit()
    return culled


def run_head_of_desk_cycle(conn, config: dict | None = None) -> dict[str, Any]:
    if config is None:
        config = _load_config()

    spawned = ensure_agent_count(conn, config)
    culled = cull_if_overpopulated(conn, config)

    return {
        "checked_at": _now(),
        "spawned": spawned,
        "culled": culled,
        "agent_count": conn.execute(
            "SELECT COUNT(*) FROM agents WHERE status IN ('rookie', 'active')"
        ).fetchone()[0],
        "distribution": get_strategy_distribution(conn),
    }
