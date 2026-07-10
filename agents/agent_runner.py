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

DEFAULT_LOG_PATH = Path("data/forge.log")


def _build_llm_fn(config: dict):
    """Build the llm_fn callable passed to run_decision().

    Must match agents/decision_loop.py's _call_llm_with_retry calling
    contract exactly: fn(system_prompt, decision_prompt, agent_id=None) ->
    (decision_dict, model_display_name_or_None). Forwarding agent_id lets
    model_chain.decide() resolve this agent's pinned model (see
    llm/model_chain.py's decide()) — without it, every agent silently falls
    through to the default chain regardless of any pin. See
    docs/STRATEGIC_ASSESSMENT_07_09_2026.md defect C1.
    """
    def llm_fn(
        system_prompt: str, decision_prompt: str, agent_id: str | None = None
    ) -> tuple[dict, str | None]:
        return model_chain.decide(
            system_prompt, decision_prompt, config=config, agent_id=agent_id
        )
    return llm_fn


def _configure_logging(log_path: Path = DEFAULT_LOG_PATH) -> None:
    """Set up stderr + persistent file logging for this agent subprocess.

    llm/model_chain.py logs why each fallback-chain tier failed (timeout,
    non-zero exit, invalid decision shape) via logger.warning(...). Without
    a file handler those reasons only exist on this subprocess's stderr,
    which forge.py only surfaces (truncated) on a non-zero exit — on a
    normal exit (falling through to a working tier), they're lost. Multiple
    agent subprocesses append to the same file concurrently; individual
    warning lines are short enough that OS-level writes don't interleave
    mid-line in practice, so no cross-process locking is used here.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


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

        llm_fn = _build_llm_fn(config)

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

    _configure_logging()

    result = asyncio.run(_run_once(args.agent_id, args.db_path, args.config_path))

    # Structured output line — forge.py's fleet cycle parses this from stdout.
    # Log lines go to stderr (logging default), so stdout is clean for parsing.
    print(_build_result_line(args.agent_id, result))

    if result.get("action") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
