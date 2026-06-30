"""
agents/decision_loop.py — Core decision pipeline for one agent wake cycle.

run_decision never raises. All exceptions are caught, logged, and returned
as {"action": "error", "detail": str(exc)}.
"""

import logging
from agents.persona import build_system_prompt
from agents.prompt_builder import build_decision_prompt
from risk.gate import validate_order, RiskViolation
from store.db import get_positions

logger = logging.getLogger(__name__)


async def run_decision(
    agent_id: str,
    thesis_text: str,
    config: dict,
    conn,
    provider,
    llm_fn,
    bridge_factory,
) -> dict:
    """Async full decision cycle for one agent wake.

    Returns {"action": str, "detail": str}. Never raises.

    Return values:
        {"action": "enter", "detail": str(fill)}       — trade opened
        {"action": "wait", "detail": reason_str}        — LLM chose to wait
        {"action": "close", "detail": str(fill)}        — position closed
        {"action": "risk_blocked", "detail": reason}    — risk gate rejected
        {"action": "error", "detail": str(exc)}         — unexpected exception
    """
    try:
        assets = config["universe"]
        desk_config = config["desk"]

        market_state = await provider.get_market_state(assets)
        system_prompt = build_system_prompt(agent_id, config)
        decision_prompt = await build_decision_prompt(
            agent_id, thesis_text, market_state, conn, provider,
            starting_balance=desk_config["starting_balance"],
        )

        response = llm_fn(system_prompt, decision_prompt)

        action = response.get("action", "wait")

        if action == "wait":
            reason = response.get("reason", "")
            logger.info("[%s] LLM decided to wait: %s", agent_id, reason)
            return {"action": "wait", "detail": reason}

        if action == "close":
            pos_id = response.get("position_id")
            reason = response.get("reason", "agent_close")
            bridge = bridge_factory(agent_id, conn, provider)
            fill = await bridge.close(pos_id, reason)
            logger.info("[%s] Closed position %s: %s", agent_id, pos_id, fill)
            return {"action": "close", "detail": str(fill)}

        if action == "enter":
            open_positions = get_positions(conn, agent_id)
            try:
                validate_order(
                    order=response,
                    account_balance=_get_balance(conn, agent_id, desk_config["starting_balance"]),
                    config=desk_config,
                    open_position_count=len(open_positions),
                )
            except RiskViolation as e:
                logger.warning("[%s] Risk gate blocked order: %s", agent_id, e.reason)
                return {"action": "risk_blocked", "detail": e.reason}

            bridge = bridge_factory(agent_id, conn, provider)
            fill = await bridge.enter(response)
            logger.info("[%s] Entered trade: %s", agent_id, fill)
            return {"action": "enter", "detail": str(fill)}

        logger.warning("[%s] Unrecognized LLM action '%s', treating as wait", agent_id, action)
        return {"action": "wait", "detail": f"unrecognized LLM action: {action}"}

    except Exception as exc:
        logger.error("[%s] Decision loop error: %s", agent_id, exc, exc_info=True)
        return {"action": "error", "detail": str(exc)}


def _get_balance(conn, agent_id: str, starting_balance: float) -> float:
    from store.db import get_latest_account
    latest = get_latest_account(conn, agent_id, "paper")
    return latest["balance"] if latest else starting_balance
