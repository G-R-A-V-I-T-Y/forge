"""Forge — single entrypoint. Starts agent scheduler + web server in one process."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from store.db import get_connection, init_schema, insert_agent, insert_account_snapshot
from store.positions import get_all_open_positions
from market.provider import MarketProvider
from llm import client as llm_client
from execution.paper_bridge import PaperBridge
from agents.runtime import AgentRuntime
from web.app import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forge")

DB_PATH = Path("data/forge.db")
CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def get_agent_wake_interval(agent_row: dict, desk_config: dict) -> int:
    """Read per-agent wake interval from config_json, fall back to desk default."""
    default = desk_config.get("wake_interval_seconds", 60)
    try:
        overrides = json.loads(agent_row.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return default
    return overrides.get("wake_interval", default)


async def main():
    config = load_config()
    desk_config = config["desk"]

    provider = MarketProvider(config)
    await provider.__aenter__()

    def bridge_factory(agent_id: str, conn, provider) -> PaperBridge:
        return PaperBridge(agent_id=agent_id, conn=conn, provider=provider, config=config)

    def llm_fn(system_prompt: str, decision_prompt: str) -> dict:
        return llm_client.decide(system_prompt, decision_prompt, config=config)

    conn = get_connection(str(DB_PATH))
    init_schema(conn)

    # Seed jade_hawk if the DB is empty (first run compatibility)
    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    if agent_count == 0:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_agent(conn, "jade_hawk", "jade_hawk", now, "{}")
        balance = desk_config["starting_balance"]
        insert_account_snapshot(conn, "jade_hawk", "paper", balance, balance)
        logger.info("Seeded default agent jade_hawk")

    web_app.state.conn = conn
    web_app.state.provider = provider
    web_app.state.config = config

    # Restore open positions from SQLite (position registry at startup)
    open_positions = get_all_open_positions(conn)
    logger.info("Restored %d open positions across the desk", len(open_positions))

    # Read all active/rookie agents from SQLite
    agent_rows = conn.execute(
        "SELECT * FROM agents WHERE status IN ('rookie', 'active') ORDER BY name"
    ).fetchall()

    scheduler = AsyncIOScheduler()
    base_interval = desk_config.get("wake_interval_seconds", 60)

    for idx, row in enumerate(agent_rows):
        agent = dict(row)
        agent_id = agent["id"]
        thesis_version = agent.get("current_thesis_version", 1)
        thesis_path = Path("agents/theses") / f"{agent_id}_v{thesis_version}.md"

        per_agent_interval = get_agent_wake_interval(agent, desk_config)

        runtime = AgentRuntime(
            agent_id=agent_id,
            thesis_path=str(thesis_path),
            config=config,
            conn=conn,
            provider=provider,
            llm_fn=llm_fn,
            bridge_factory=bridge_factory,
            scheduler=scheduler,
        )

        # Stagger start: agent_index × 30s offset from the base interval
        offset_seconds = idx * 30
        start_date = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)

        trigger = IntervalTrigger(
            seconds=per_agent_interval,
            start_date=start_date,
            timezone=timezone.utc,
        )
        scheduler.add_job(
            runtime.tick,
            trigger=trigger,
            id=agent_id,
            replace_existing=True,
        )
        logger.info(
            "Scheduled %s — wakes every %ds (first wake in %ds)",
            agent_id, per_agent_interval, offset_seconds,
        )

    scheduler.start()
    logger.info("Scheduler started with %d agents", len(agent_rows))

    server_config = uvicorn.Config(web_app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(server_config)
    logger.info("Web UI starting at http://localhost:8000")

    try:
        await server.serve()
    finally:
        await provider.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
