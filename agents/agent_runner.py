"""agents/agent_runner.py — standalone agent decision cycle.

Invoked as a subprocess by forge.py's fleet cycle so every agent gets its
own process and calls opencode independently — true OS-level parallelism:

    python -m agents.agent_runner --agent-id <id> --db-path <path> --config-path <path>

Reads the shared heartbeat file, builds prompts, calls model_chain.decide()
(synchronous — fine here because this whole process is one-shot), executes
the trade decision (enter/close/wait), prints a structured result line, and
exits.  Never raises or re-raises; all errors are caught, logged, and
surfaced as structured output so forge.py's fleet cycle can inspect the
outcome.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from agents.decision_loop import run_decision
from execution.paper_bridge import PaperBridge
from llm import model_chain
from market.provider import MarketProvider
from store.db import get_connection, get_agent

logger = logging.getLogger(__name__)


def _resolve_thesis(agent_id: str, agent_row: dict) -> str:
    version = agent_row.get("current_thesis_version", 1)
    thesis_path = Path("agents/theses") / f"{agent_id}_v{version}.md"
    try:
        return thesis_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[%s] Thesis not found at %s", agent_id, thesis_path)
        return "No thesis loaded."


async def _run_once(agent_id: str, db_path: str, config_path: str) -> dict:
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

    conn = get_connection(db_path)
    try:
        agent_row = get_agent(conn, agent_id)
        if agent_row is None:
            return {"action": "error", "detail": "agent not found in database"}
        thesis_text = _resolve_thesis(agent_id, agent_row)

        def llm_fn(system_prompt: str, decision_prompt: str) -> tuple[dict, str | None]:
            return model_chain.decide(system_prompt, decision_prompt, config=config)

        def bridge_factory(aid: str, c, provider):
            return PaperBridge(aid, conn=c, provider=provider, config=config)

        async with MarketProvider(config) as provider:
            result = await run_decision(
                agent_id=agent_id,
                thesis_text=thesis_text,
                config=config,
                conn=conn,
                provider=provider,
                llm_fn=llm_fn,
                bridge_factory=bridge_factory,
            )
    finally:
        conn.close()

    return result


def _build_result_line(agent_id: str, result: dict) -> str:
    action = result.get("action", "?")
    detail = (result.get("detail") or "")[:200]
    return f"AGENT_RESULT [{agent_id}] action={action} detail={detail}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one agent's decision cycle as a standalone process."
    )
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--db-path", default="data/forge.db")
    parser.add_argument("--config-path", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = asyncio.run(_run_once(args.agent_id, args.db_path, args.config_path))

    # Structured output line — forge.py's fleet cycle parses this from stdout.
    # Log lines go to stderr (logging default), so stdout is clean for parsing.
    print(_build_result_line(args.agent_id, result))

    if result.get("action") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
