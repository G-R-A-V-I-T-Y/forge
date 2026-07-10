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
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from llm.llama_server import server_manager as llama_server
from market import heartbeat
from market.provider import MarketProvider
from store.db import get_connection, init_schema, insert_account_snapshot, insert_agent
from store.git_sync import sync_to_git
from store.positions import (
    get_all_open_positions,
    reconcile_positions,
    update_position_pnl,
)
from store.settings import load_all as load_settings
from store.state_snapshot import write_current_state
from web.app import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forge")

DB_PATH = Path("data/forge.db")
CONFIG_PATH = Path("config.yaml")

_SAGE_TURTLE_THESIS = """\
# sage_turtle -- Thesis v1: Event & Unlock Positioning

## Edge Hypothesis

Scheduled supply and macro events are public, dated, and repeatedly under-anticipated by the market until they are imminent. Token unlocks release a known quantity of new supply to holders (often early investors/team with a low cost basis and a high propensity to sell) at a known timestamp; the market chronically underprices the sell pressure until the unlock is within days, then overcorrects. Macro events (FOMC, CPI) do not move any single asset's supply, but they reset the funding/leverage backdrop for the entire book in ways theses cannot see coming. This agent does not predict price from price -- it predicts price from the calendar: what is scheduled, how large is it relative to float, and how has the market historically reacted to this specific event type.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule -- wait, but log to the watchlist if an event is within 10 days

## Position Parameters

- Direction: Short into large unlocks (dilution); long only in the rare case of a documented buyback/burn event with equivalent evidence structure inverted.
- Leverage: 3x
- Position size: 10% of account per trade
- Stop loss: 3.0% from entry
- Take profit: 6.0% from entry
- Max hold time: through the event plus 24 hours, then exit regardless of P&L
"""


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
            write_current_state(conn)
        except Exception:
            logger.warning(
                "Failed to update position PnL from heartbeat", exc_info=True
            )
        finally:
            conn.close()


async def _spawn_agent_runner(agent_id: str, db_path: str, config_path: str) -> dict:
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

    # Must exceed the worst-case total time for llm/model_chain.py's
    # fallback chain: up to ~4 opencode tiers can hang/fail for the full
    # OPENCODE_TIMEOUT_SECS (60s) each before falling through, plus the
    # Ollama tier's own TIMEOUT_SECS (900s, see llm/ollama_client.py) —
    # otherwise a real (but slow, e.g. queued behind other concurrent
    # agents) Qwen answer gets killed here before it's ever captured.
    AGENT_RUNNER_TIMEOUT_SECS = 1200
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=AGENT_RUNNER_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning(
            "[%s] Agent runner timed out after %ds", agent_id, AGENT_RUNNER_TIMEOUT_SECS
        )
        return {"agent_id": agent_id, "action": "timeout", "detail": ""}

    out_text = stdout.decode("utf-8", errors="replace")

    if proc.returncode not in (0, None):
        err_text = stderr.decode("utf-8", errors="replace")[:300]
        logger.warning(
            "[%s] Agent runner exited %d: %.300s",
            agent_id,
            proc.returncode,
            err_text,
        )

    # Parse the structured result line (last AGENT_RESULT line wins)
    result: dict | None = None
    for line in out_text.splitlines():
        if line.startswith("AGENT_RESULT"):
            rest = line[len("AGENT_RESULT ") :]
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

    logger.info("Fleet cycle: spawning %d agent(s) in parallel", len(agent_ids))

    tasks = [_spawn_agent_runner(aid, db_path, config_path) for aid in agent_ids]
    results = await asyncio.gather(*tasks)

    for r in results:
        detail = r.get("detail", "")
        logger.info(
            "[%s] Result: %s — %.200s",
            r["agent_id"],
            r["action"],
            detail,
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

    from store.db import void_corrupted_trades
    voided = void_corrupted_trades(conn)
    if voided:
        logger.info("Voided %d corrupted trade(s) from pre-M6 schema", voided)

    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    if agent_count == 0:
        TRADER_NAMES = [
            "iron_moth",
            "silver_basin",
            "copper_vane",
            "gray_finch",
            "amber_wolf",
            "steel_crane",
            "onyx_heron",
            "jade_hawk",
            "violet_lion",
            "crimson_fox",
        ]
        balance = desk_config.get("starting_balance", 50000.0)
        for name in TRADER_NAMES:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            insert_agent(conn, name, name, now, "{}")
            insert_account_snapshot(conn, name, "paper", balance, balance)
        logger.info(
            "Seeded %d default agents with $%.0f each", len(TRADER_NAMES), balance
        )

    # M8: Retire gray_finch and amber_wolf (microstructure agents confirmed unviable)
    for _retire_id in ("gray_finch", "amber_wolf"):
        conn.execute(
            "UPDATE agents SET status = 'terminated' WHERE id = ? AND status != 'terminated'",
            (_retire_id,),
        )
    conn.commit()

    # M8: Spawn sage_turtle (compiled event/unlock agent) if not already present
    if not conn.execute("SELECT id FROM agents WHERE id = 'sage_turtle'").fetchone():
        _now_s = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _cfg_json = json.dumps({"compiled": True, "wake_interval": 300})
        conn.execute(
            """INSERT INTO agents (id, name, status, spawn_date, config_json, current_thesis_version)
               VALUES (?, ?, ?, ?, ?, 1)""",
            ("sage_turtle", "sage_turtle", "rookie", _now_s, _cfg_json),
        )
        insert_account_snapshot(
            conn, "sage_turtle", "paper",
            desk_config.get("starting_balance", 50000.0),
            desk_config.get("starting_balance", 50000.0),
        )
        _thesis_path = Path("agents/theses") / "sage_turtle_v1.md"
        _thesis_path.parent.mkdir(parents=True, exist_ok=True)
        _thesis_path.write_text(_SAGE_TURTLE_THESIS, encoding="utf-8")
        conn.commit()
        logger.info("Spawned sage_turtle (compiled event/unlock agent)")

    web_app.state.conn = conn
    web_app.state.provider = provider
    web_app.state.config = config
    web_app.state.llama_server = llama_server

    # Start the local llama-server if configured.
    local_settings = load_settings(conn)
    if local_settings.get("spawn_on_startup"):
        logger.info("spawn_on_startup=true — starting local llama-server")
        llama_server.start(local_settings)
    else:
        logger.info("spawn_on_startup=false — local llama-server not started")

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
    # Counterfactual analysis — runs nightly at 02:00 UTC.
    # Analyzes past "wait" decisions to determine if taking the trade
    # would have been profitable.
    # ------------------------------------------------------------------
    async def _run_counterfactual_job():
        """Run counterfactual analysis for all agents."""
        try:
            agents = conn.execute("SELECT id, name FROM agents").fetchall()
            for agent in agents:
                agent_id = agent["id"]
                agent_name = agent["name"]
                logger.info(
                    "Running counterfactual analysis for agent %s (%s)",
                    agent_id,
                    agent_name,
                )
                # Get the system prompt for the agent
                from agents.persona import build_system_prompt

                system_prompt = build_system_prompt(agent_id, config)
                from agents.decision_loop import run_counterfactual

                await run_counterfactual(
                    conn,
                    agent_id,
                    None,
                    lambda sp, dp, **kw: llm_fn(sp, dp),
                    system_prompt,
                )
        except Exception as exc:
            logger.error("Counterfactual analysis failed: %s", exc, exc_info=True)

    scheduler.add_job(
        _run_counterfactual_job,
        trigger="cron",
        hour=2,
        minute=0,
        id="counterfactual",
        replace_existing=True,
    )
    logger.info("Counterfactual analysis job scheduled — runs nightly at 02:00 UTC")

    # ------------------------------------------------------------------
    # Ledger git sync -- commits + pushes ledger/ and state/ every cycle.
    # Best-effort: a failed push just retries next cycle (see
    # store/git_sync.py). Runs on the heartbeat cadence so it never lags
    # more than one cycle behind what agents actually wrote.
    # ------------------------------------------------------------------
    async def _run_git_sync_job():
        try:
            committed = await asyncio.get_event_loop().run_in_executor(
                None, sync_to_git, Path(__file__).resolve().parent
            )
            if committed:
                logger.info("Ledger git sync: committed and pushed")
        except Exception:
            logger.warning("Ledger git sync job failed", exc_info=True)

    scheduler.add_job(
        _run_git_sync_job,
        trigger=IntervalTrigger(seconds=heartbeat_interval, timezone=timezone.utc),
        id="ledger_git_sync",
        replace_existing=True,
    )
    logger.info("Ledger git sync scheduler started -- runs every %ds", heartbeat_interval)

    # ------------------------------------------------------------------
    # Ledger compaction -- runs monthly, converts the PRIOR month's closed
    # JSONL partitions to Parquet (with resolution decay for old
    # candles_5m/oi). Without this, ledger_git_sync above commits an
    # ever-growing current-month JSONL every cycle with no rollup ever
    # firing -- compaction is load-bearing for repo-size control, not
    # optional housekeeping. See scripts/compact_ledger.py.
    # ------------------------------------------------------------------
    async def _run_compaction_job():
        try:
            from scripts.compact_ledger import compact_ledger

            written = await asyncio.get_event_loop().run_in_executor(None, compact_ledger)
            if written:
                logger.info("Ledger compaction: compacted %d file(s)", len(written))
        except Exception:
            logger.warning("Ledger compaction job failed", exc_info=True)

    scheduler.add_job(
        _run_compaction_job,
        trigger="cron",
        day=1,
        hour=3,
        minute=0,
        id="ledger_compaction",
        replace_existing=True,
    )
    logger.info("Ledger compaction job scheduled -- runs monthly on day 1 at 03:00 UTC")

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
        agent_count,
        wake_interval,
    )

    # ------------------------------------------------------------------
    # Web server
    # ------------------------------------------------------------------
    server_config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)
    logger.info("Web UI starting at http://localhost:8000")

    try:
        await asyncio.gather(server.serve(), fleet_task)
    finally:
        await provider.__aexit__(None, None, None)
        llama_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
