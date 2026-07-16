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
import re
import sys
from datetime import timezone
from pathlib import Path

import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backtest.dsl import load_spec
from llm.llama_server import server_manager as llama_server
from market import heartbeat
from market.provider import MarketProvider
from store.db import get_connection, init_schema
from store.git_sync import sync_to_git
from store.positions import (
    get_all_open_positions,
    reconcile_positions,
    update_position_pnl,
)
from store.settings import load_all as load_settings
from store.specs import SPECS_DIR, deploy_spec, get_active_spec
from store.state_snapshot import write_current_state
from web.app import app as web_app

# M9: Selection & Daily Improvement Loop
# (head_of_desk deliberately not imported — its job is latched off until R7;
# see the disabled-job note below.)
from meta.controller import evaluate_agent, run_evaluation_cycle
from meta.reflection_scheduler import (
    check_agent_eligible,
    get_reflection_trigger,
)
from meta.risk_officer import risk_check_cycle

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
        from scripts.fresh_start import seed_desk
        seeded = seed_desk(conn, config)
        logger.info("Seeded %d agents via seed_desk", len(seeded))

    # Retire gray_finch and amber_wolf (microstructure agents confirmed unviable)
    for _retire_id in ("gray_finch", "amber_wolf"):
        conn.execute(
            "UPDATE agents SET status = 'terminated' WHERE id = ? AND status != 'terminated'",
            (_retire_id,),
        )
    conn.commit()

    # Reconcile compiled agents: for every active/rookie agent whose
    # config_json marks it as compiled, check whether a spec file exists
    # on disk and no active spec row is in the DB.  If so, deploy the
    # latest version so the compiled decision loop has something to
    # evaluate.
    for row in conn.execute(
        "SELECT id, config_json FROM agents WHERE status IN ('rookie', 'active')"
    ).fetchall():
        agent_id = row["id"]
        try:
            agent_cfg = json.loads(row["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not agent_cfg.get("compiled"):
            continue
        if get_active_spec(conn, agent_id) is not None:
            continue

        spec_files = list(SPECS_DIR.glob(f"{agent_id}_v*.yaml"))
        if not spec_files:
            logger.warning(
                "Compiled agent %s has no spec files at %s",
                agent_id, SPECS_DIR / f"{agent_id}_v*.yaml",
            )
            continue

        latest = max(
            spec_files,
            key=lambda p: int(re.search(r"_v(\d+)\.yaml$", p.name).group(1)),
        )
        _spec = load_spec(str(latest))
        deploy_spec(conn, agent_id, _spec, config=desk_config)
        logger.info(
            "Deployed %s spec v%d (compiled-agent reconciliation)",
            agent_id, _spec.spec_version,
        )

    # M6: Seed benchmark agents (idempotent — INSERT OR IGNORE).
    from scripts.seed_benchmarks import seed_benchmark_agents
    seed_benchmark_agents(conn, config)

    web_app.state.conn = conn
    web_app.state.provider = provider
    web_app.state.config = config
    web_app.state.llama_server = llama_server

    # Expose the reflection transport so manual trigger endpoints
    # (trigger-reflection, trigger-all-reflections) can invoke it from the
    # web layer. Per M9 criterion 2, reflection must NEVER go through
    # model_chain.decide — that transport validates every response as a
    # trade decision (action ∈ enter/wait/close) and silently rejects
    # reflection output (spec YAML, diagnosis text) by construction.
    # trigger-evaluation does not use state.llm_fn at all (it calls
    # meta.controller directly).
    from llm import reflection_client
    web_app.state.llm_fn = reflection_client.complete

    # Start the local llama-server if configured.
    local_settings = load_settings(conn)
    if local_settings.get("spawn_on_startup"):
        logger.info("spawn_on_startup=true — starting local llama-server")
        llama_server.start(local_settings)
    else:
        logger.info("spawn_on_startup=false — local llama-server not started")

    # ------------------------------------------------------------------
    # R12 Pre-run safety latches — logged here so the run's operating notes
    # can record which mode was used for each latch.  Both are code-guard
    # routes (partial early landings of R7.1 / R8) and will be replaced by
    # the full R7/R8 implementations during or after the run.
    # ------------------------------------------------------------------
    logger.info(
        "R12 Latch 1 (meta-controller): code-guard active — "
        "get_lifecycle_decision never terminates for 'not beating null' "
        "when null distribution is None or below 30 closed trades"
    )
    logger.info(
        "R12 Latch 2 (reflection): code-guard active — "
        "run_reflection rejects any revised spec with zero evidence terms"
    )

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
    # M10: Forward labeling -- runs nightly at 02:30 UTC.
    # meta/labeling.py::run_labeling_job forward-labels every decision at
    # least LONGEST_HOURS old against recorded 5m candles (return, MFE/MAE,
    # best action, regret at 1h/4h/24h) -- feeds agents/dossier.py's
    # top-regret evidence and the /decisions coverage tiles. T7: it also
    # absorbs the M6 wait-only counterfactual filler (deterministic replay
    # of unfilled wait decisions via store/counterfactuals.py), writing the
    # legacy counterfactual_* columns for compatibility -- there is no
    # longer a separate nightly counterfactual job.
    # ------------------------------------------------------------------
    async def _run_labeling_job():
        try:
            from meta.labeling import run_labeling_job

            ledger_dir = config.get("ledger_dir", "ledger")
            summary = run_labeling_job(conn, ledger_dir, config)
            logger.info("Forward labeling complete: %s", summary)
        except Exception as exc:
            logger.error("Forward labeling job failed: %s", exc, exc_info=True)

    scheduler.add_job(
        _run_labeling_job,
        trigger="cron",
        hour=2,
        minute=30,
        id="labeling",
        replace_existing=True,
    )
    logger.info(
        "Forward labeling job scheduled — runs nightly at 02:30 UTC "
        "(absorbs the M6 counterfactual filler)"
    )

    # ------------------------------------------------------------------
    # M10: Training dataset refresh -- runs nightly at 03:30 UTC.
    # scripts/build_training_dataset.py is an offline, read-only batch job
    # that flattens ledger history into
    # data/historical_data/training_dataset.parquet for feature-conditioned
    # stats (agents/dossier.py) and future model training. Never wired into
    # the live decision loop -- log-and-continue on failure so a bad or
    # stale ledger partition can never break the fleet cycle.
    # ------------------------------------------------------------------
    async def _run_training_dataset_job():
        try:
            from scripts.build_training_dataset import build_dataset

            df = await asyncio.get_event_loop().run_in_executor(None, build_dataset)
            logger.info("Training dataset refresh complete: %d row(s)", len(df))
        except Exception as exc:
            logger.error("Training dataset refresh failed: %s", exc, exc_info=True)

    scheduler.add_job(
        _run_training_dataset_job,
        trigger="cron",
        hour=3,
        minute=30,
        id="training_dataset_refresh",
        replace_existing=True,
    )
    logger.info("Training dataset refresh job scheduled — runs nightly at 03:30 UTC")

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
    # M9: Meta-controller — evaluation cycle for all active agents.
    # Runs every 30 minutes on the trade-count cadence (checks if each
    # agent is due, enforces lifecycle rules, harvests on termination).
    # ------------------------------------------------------------------
    async def _run_meta_controller_job():
        try:
            conn = get_connection(str(DB_PATH))
            try:
                results = run_evaluation_cycle(conn)
                suspensions = [r for r in results if r.get("decision") == "suspend"]
                terminations = [r for r in results if r.get("decision") == "terminate"]
                if suspensions or terminations:
                    logger.info(
                        "Meta-controller: %d suspension(s), %d termination(s) out of %d agent(s)",
                        len(suspensions), len(terminations), len(results),
                    )
            finally:
                conn.close()
        except Exception:
            logger.warning("Meta-controller cycle failed", exc_info=True)

    scheduler.add_job(
        _run_meta_controller_job,
        trigger=IntervalTrigger(minutes=30, timezone=timezone.utc),
        id="meta_controller",
        replace_existing=True,
    )
    logger.info("Meta-controller job scheduled — runs every 30 min")

    # ------------------------------------------------------------------
    # M9: Risk officer — central risk oversight.
    # Runs on a 30-min cadence (criterion: "30-60 min cadence") to check
    # desk kill switch, concentration, per-agent limits, gross-exposure
    # throttle, event-calendar blackout, and produce the regime memo.
    # Nothing else in the codebase currently reads entry_disables /
    # is_entry_gate_open on a tighter, independently-wired cadence, so the
    # whole cycle moves to 30 min as a unit — see meta/risk_officer.py.
    # ------------------------------------------------------------------
    async def _run_risk_officer_job():
        try:
            conn = get_connection(str(DB_PATH))
            try:
                report = risk_check_cycle(conn, config)
                if report.get("desk_kill_switch"):
                    logger.warning("Risk officer: DESK KILL SWITCH ACTIVE — all entries blocked")
                if report.get("concentration_violators"):
                    logger.warning(
                        "Risk officer: concentration violators: %s",
                        ", ".join(report["concentration_violators"]),
                    )
                if report.get("gross_exposure_throttled_agents"):
                    logger.warning(
                        "Risk officer: gross exposure throttle disabled entries for: %s",
                        ", ".join(report["gross_exposure_throttled_agents"]),
                    )
                if report.get("event_blackout"):
                    logger.warning(
                        "Risk officer: event blackout active (%s) — all entries blocked",
                        report["event_blackout"].get("name"),
                    )
            finally:
                conn.close()
        except Exception:
            logger.warning("Risk officer cycle failed", exc_info=True)

    scheduler.add_job(
        _run_risk_officer_job,
        trigger=IntervalTrigger(minutes=30, timezone=timezone.utc),
        id="risk_officer",
        replace_existing=True,
    )
    logger.info("Risk officer job scheduled — runs every 30 min")

    # ------------------------------------------------------------------
    # M9: Reflection scheduler — checks agent eligibility and triggers
    # reflection cycles for agents that have crossed their threshold.
    # Runs every 30 minutes.
    # ------------------------------------------------------------------
    async def _run_reflection_scheduler_job():
        try:
            import sqlite3
            conn = get_connection(str(DB_PATH))
            try:
                trigger_cfg = get_reflection_trigger(conn)
                if trigger_cfg.get("mode") == "manual":
                    return

                rows = conn.execute(
                    "SELECT id FROM agents WHERE status IN ('rookie', 'active') ORDER BY name"
                ).fetchall()

                for row in rows:
                    agent_id = row["id"]
                    eligible, reason = check_agent_eligible(conn, agent_id, trigger_cfg)
                    if not eligible:
                        continue

                    logger.info("Reflection due for agent %s — starting cycle", agent_id)

                    # Use the reflection_client transport that returns raw text
                    # with NO decision-schema validation (mc_decide was rejecting
                    # every reflection by construction — see defect D1 in
                    # docs/STRATEGIC_ASSESSMENT_M9_M10.md).
                    from llm.reflection_client import complete as reflection_complete
                    try:
                        from meta.reflection_scheduler import run_reflection_cycle as _run_reflection
                        _run_reflection(conn, agent_id, config, reflection_complete)
                    except Exception as exc:
                        logger.error(
                            "Reflection failed for %s: %s", agent_id, exc, exc_info=True,
                        )
            finally:
                conn.close()
        except Exception:
            logger.warning("Reflection scheduler job failed", exc_info=True)

    scheduler.add_job(
        _run_reflection_scheduler_job,
        trigger=IntervalTrigger(minutes=30, timezone=timezone.utc),
        id="reflection_scheduler",
        replace_existing=True,
    )
    logger.info("Reflection scheduler job scheduled — runs every 30 min")

    # ------------------------------------------------------------------
    # M10: Challenger resolution (criterion 5+6) — for every agent with a
    # deployed challenger, agents/reflection.py::apply_challenger_resolution
    # applies the desk.challenger_min_decisions / desk.challenger_max_days
    # trigger (check_challenger_resolution, which counts LABELED decisions)
    # and, once due, resolves the trial via store.specs.resolve_challenger
    # (mean labeled regret over the trial window), resolves the cycle's
    # hypotheses via resolve_hypotheses, AND writes reflections.outcome
    # unconditionally — including for cycles that registered zero
    # hypotheses (T8 review r2 Fix B). The per-agent body lives in
    # agents/reflection.py so it is testable without importing this module
    # (apscheduler is not installed in the test env). config["desk"] is
    # read directly (not .get) so a missing desk section fails loudly
    # (logged) rather than silently defaulting. Cheap and idempotent when
    # nothing is due, so an hourly cadence is plenty.
    # ------------------------------------------------------------------
    async def _run_challenger_resolution_job():
        try:
            from agents.reflection import apply_challenger_resolution

            conn = get_connection(str(DB_PATH))
            try:
                desk_config = config["desk"]
                rows = conn.execute(
                    "SELECT DISTINCT agent_id FROM specs WHERE status = 'challenger'"
                ).fetchall()
                for row in rows:
                    agent_id = row["agent_id"]
                    result = apply_challenger_resolution(conn, agent_id, desk_config)
                    if result.get("resolved"):
                        logger.info(
                            "Challenger resolution for %s: verdict=%s",
                            agent_id, result.get("verdict"),
                        )
            finally:
                conn.close()
        except Exception:
            logger.warning("Challenger resolution job failed", exc_info=True)

    scheduler.add_job(
        _run_challenger_resolution_job,
        trigger=IntervalTrigger(hours=1, timezone=timezone.utc),
        id="challenger_resolution",
        replace_existing=True,
    )
    logger.info("Challenger resolution job scheduled — runs hourly")

    # ------------------------------------------------------------------
    # M9: Head of desk — daily briefing (crit 8). The old auto-spawner
    # this job once latched off (pre-run finding F8) was replaced by the
    # briefing/chat Head of Desk; population management now lives with
    # the evaluator/spawner path, not here.
    # ------------------------------------------------------------------
    async def _run_daily_briefing_job():
        try:
            from meta.head_of_desk import generate_morning_brief, store_briefing
            conn = get_connection(str(DB_PATH))
            try:
                brief = generate_morning_brief(conn, config)
                store_briefing(conn, brief)
                logger.info(
                    "Morning brief stored for %s (%d agents, %d alerts)",
                    brief.get("date"),
                    len(brief.get("agents_covered", [])),
                    brief.get("summary", {}).get("alert_count", 0),
                )
            finally:
                conn.close()
        except Exception:
            logger.warning("Daily briefing job failed", exc_info=True)

    scheduler.add_job(
        _run_daily_briefing_job,
        trigger=CronTrigger(hour=6, minute=0, timezone=timezone.utc),
        id="daily_briefing",
        replace_existing=True,
    )
    logger.info("Head of Desk daily briefing job scheduled — 06:00 UTC")

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
