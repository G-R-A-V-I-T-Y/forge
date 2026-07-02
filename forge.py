"""Forge — single entrypoint. Heartbeat + parallel agent fleet + web server.

Architecture:
  • Heartbeat runs as an independent APScheduler job — never blocked by agents.
  • Agents run as standalone subprocesses (agents/agent_runner.py), all spawned
    simultaneously every wake_interval_seconds via asyncio.gather.  Each agent
    calls opencode in its own process — true OS-level parallelism.
  • Web server runs alongside both.

This replaces the old design where every agent shared the scheduler's event
loop with synchronous model_chain.decide() calls, which blocked the loop and
starved the heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from market import heartbeat
from market.provider import MarketProvider
from store.db import get_connection, init_schema, insert_agent, insert_account_snapshot
from store.positions import get_all_open_positions, reconcile_positions, update_position_pnl
from web.app import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forge")

DB_PATH = Path("data/forge.db")
CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


async def run_heartbeat_cycle(provider, config: dict) -> None:
    """One heartbeat generation cycle — wrapped for both the immediate
    startup run and the recurring APScheduler job."""
    packet = await heartbeat.generate_heartbeat(provider, config)
    logger.info(
        "Heartbeat cycle complete: %d assets written at %s",
        len(packet.get("assets", {})),
        packet.get("timestamp"),
    )
    assets_data = packet.get("assets", {})
    if assets_data:
        conn = get_connection(str(DB_PATH))
        try:
            closed = await reconcile_positions(conn, assets_data, provider, config)
            if closed:
                logger.info("SL/TP reconciled %d position(s)", closed)
            update_position_pnl(conn, assets_data)
        except Exception:
            logger.warning(
                "Failed to update position PnL from heartbeat", exc_info=True
            )
        finally:
            conn.close()


async def _spawn_agent_runner(
    agent_id: str, db_path: str, config_path: str
) -> dict:
    """Run one agent as a standalone subprocess and return its result dict.

    The agent process reads the shared heartbeat file, calls model_chain
    (opencode subprocess), executes the decision via PaperBridge, and prints
    a structured ``AGENT_RESULT`` line on stdout that we parse here.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agents.agent_runner",
        "--agent-id",
        agent_id,
        "--db-path",
        db_path,
        "--config-path",
        config_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=600
        )
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning("[%s] Agent runner timed out after 600s", agent_id)
        return {"agent_id": agent_id, "action": "timeout", "detail": ""}

    out_text = stdout.decode("utf-8", errors="replace")

    if proc.returncode not in (0, None):
        err_text = stderr.decode("utf-8", errors="replace")[:300]
        logger.warning(
            "[%s] Agent runner exited %d: %.300s",
            agent_id, proc.returncode, err_text,
        )

    # Parse the structured result line (last AGENT_RESULT line wins)
    result: dict | None = None
    for line in out_text.splitlines():
        if line.startswith("AGENT_RESULT"):
            rest = line[len("AGENT_RESULT "):]
            # rest format: [agent_id] action=... detail=...
            try:
                meta, action_part, detail_part = rest.split(None, 2)
                agent = meta.strip("[]")
                action = action_part.split("=", 1)[1] if "=" in action_part else "?"
                detail = detail_part.split("=", 1)[1] if "=" in detail_part else ""
                result = {"agent_id": agent, "action": action, "detail": detail}
            except ValueError:
                continue

    if result is None:
        result = {
            "agent_id": agent_id,
            "action": "unknown",
            "detail": out_text[:200],
        }
    return result


async def agent_fleet_cycle(config: dict) -> None:
    """Spawn every active/rookie agent as a parallel subprocess.

    All agent_runner subprocesses are launched simultaneously and run
    concurrently — each gets its own opencode session in its own process.
    """
    db_path = str(DB_PATH)
    config_path = str(CONFIG_PATH)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM agents WHERE status IN ('rookie', 'active') ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    agent_ids = [r["id"] for r in rows]

    logger.info(
        "Fleet cycle: spawning %d agent(s) in parallel", len(agent_ids)
    )

    tasks = [_spawn_agent_runner(aid, db_path, config_path) for aid in agent_ids]
    results = await asyncio.gather(*tasks)

    for r in results:
        detail = r.get("detail", "")
        logger.info(
            "[%s] Result: %s — %.200s",
            r["agent_id"], r["action"], detail,
        )


async def main():
    config = load_config()
    desk_config = config["desk"]

    provider = MarketProvider(config)
    await provider.__aenter__()

    # Run one heartbeat before the loop starts so agents immediately have
    # fresh data on their first wake.
    await run_heartbeat_cycle(provider, config)

    conn = get_connection(str(DB_PATH))
    init_schema(conn)

    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    if agent_count == 0:
        TRADER_NAMES = [
            "jade_hawk", "crimson_fox", "amber_wolf", "cobalt_raven", "emerald_bear",
            "silver_phoenix", "scarlet_viper", "golden_lion", "frost_unicorn", "storm_griffin",
        ]
        balance = desk_config.get("starting_balance", 1000.0)
        for name in TRADER_NAMES:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            insert_agent(conn, name, name, now, "{}")
            insert_account_snapshot(conn, name, "paper", balance, balance)
        logger.info("Seeded %d default agents with $%.0f each", len(TRADER_NAMES), balance)

    web_app.state.conn = conn
    web_app.state.provider = provider
    web_app.state.config = config

    open_positions = get_all_open_positions(conn)
    logger.info("Restored %d open positions across the desk", len(open_positions))

    # ------------------------------------------------------------------
    # Heartbeat — independent APScheduler job.  No agent code runs on
    # this scheduler, so the heartbeat can never be delayed.
    # ------------------------------------------------------------------
    heartbeat_interval = desk_config.get(
        "heartbeat_interval_seconds",
        heartbeat.DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    )
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_heartbeat_cycle,
        trigger=IntervalTrigger(seconds=heartbeat_interval, timezone=timezone.utc),
        args=[provider, config],
        id="heartbeat",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Heartbeat scheduler started — runs every %ds", heartbeat_interval)

    # ------------------------------------------------------------------
    # Agent fleet — independent asyncio loop.  Every wake_interval all
    # agents are spawned as parallel subprocesses.
    # ------------------------------------------------------------------
    wake_interval = desk_config.get("wake_interval_seconds", 300)

    async def _fleet_loop():
        while True:
            await agent_fleet_cycle(config)
            await asyncio.sleep(wake_interval)

    fleet_task = asyncio.create_task(_fleet_loop())
    logger.info(
        "Agent fleet cycle started — %d agent(s) wake every %ds",
        agent_count, wake_interval,
    )

    # ------------------------------------------------------------------
    # Web server
    # ------------------------------------------------------------------
    server_config = uvicorn.Config(
        web_app, host="0.0.0.0", port=8000, log_level="warning",
    )
    server = uvicorn.Server(server_config)
    logger.info("Web UI starting at http://localhost:8000")

    try:
        await asyncio.gather(server.serve(), fleet_task)
    finally:
        await provider.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
