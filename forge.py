"""Forge — single entrypoint. Starts agent scheduler + web server in one process."""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from store.db import get_connection, init_schema, insert_agent, insert_account_snapshot
from market.provider import MarketProvider
from llm.stub import decide
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


def setup_agent(conn, agent_id: str, config: dict) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    insert_agent(conn, agent_id, agent_id, now, "{}")
    existing = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE agent_id = ? AND mode = 'paper'",
        (agent_id,),
    ).fetchone()[0]
    if existing == 0:
        balance = config["desk"]["starting_balance"]
        insert_account_snapshot(conn, agent_id, "paper", balance, balance)
    logger.info("Agent %s ready", agent_id)


async def main():
    config = load_config()

    provider = MarketProvider(config)
    await provider.__aenter__()

    def bridge_factory(agent_id: str, conn, provider) -> PaperBridge:
        return PaperBridge(agent_id=agent_id, conn=conn, provider=provider, config=config)

    conn = get_connection(str(DB_PATH))
    init_schema(conn)

    agent_id = "jade_hawk"
    setup_agent(conn, agent_id, config)

    web_app.state.conn = conn
    web_app.state.provider = provider
    web_app.state.config = config

    thesis_path = Path("agents/theses/jade_hawk_v1.md")
    runtime = AgentRuntime(
        agent_id=agent_id,
        thesis_path=str(thesis_path),
        config=config,
        conn=conn,
        provider=provider,
        llm_fn=decide,
        bridge_factory=bridge_factory,
    )

    wake_seconds = config["desk"]["wake_interval_seconds"]
    scheduler = AsyncIOScheduler()
    scheduler.add_job(runtime.tick, "interval", seconds=wake_seconds, id=agent_id)
    scheduler.start()
    logger.info("Scheduler started — %s wakes every %ds", agent_id, wake_seconds)

    server_config = uvicorn.Config(web_app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(server_config)
    logger.info("Web UI starting at http://localhost:8000")

    await server.serve()

    await provider.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
