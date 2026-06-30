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


def run_decision(
    agent_id: str,
    thesis_text: str,
    config: dict,
    conn,
    get_market_fn,      # callable: (assets: list[str]) -> dict
    llm_fn,             # callable: (system_prompt: str, decision_prompt: str) -> dict
    bridge_factory,     # callable: (agent_id: str, conn, market_state: dict) -> TradingBridge
) -> dict:
    """Full decision cycle for one agent wake.

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

        # 1. Fetch market state
        market_state = get_market_fn(assets)

        # 2. Build prompts
        system_prompt = build_system_prompt(agent_id, config)
        decision_prompt = build_decision_prompt(agent_id, thesis_text, market_state, conn)

        # 3. Call LLM
        response = llm_fn(system_prompt, decision_prompt)

        action = response.get("action", "wait")

        if action == "wait":
            reason = response.get("reason", "")
            logger.info("[%s] LLM decided to wait: %s", agent_id, reason)
            return {"action": "wait", "detail": reason}

        if action == "close":
            pos_id = response.get("position_id")
            reason = response.get("reason", "agent_close")
            bridge = bridge_factory(agent_id, conn, market_state)
            fill = bridge.close(pos_id, reason)
            logger.info("[%s] Closed position %s: %s", agent_id, pos_id, fill)
            return {"action": "close", "detail": str(fill)}

        if action == "enter":
            # 4. Risk gate
            open_positions = get_positions(conn, agent_id)
            try:
                validate_order(
                    order=response,
                    account_balance=_get_balance(conn, agent_id),
                    config=desk_config,
                    open_position_count=len(open_positions),
                )
            except RiskViolation as e:
                logger.warning("[%s] Risk gate blocked order: %s", agent_id, e.reason)
                return {"action": "risk_blocked", "detail": e.reason}

            # 5. Execute via bridge
            bridge = bridge_factory(agent_id, conn, market_state)
            fill = bridge.enter(response)
            logger.info("[%s] Entered trade: %s", agent_id, fill)
            return {"action": "enter", "detail": str(fill)}

        logger.warning("[%s] Unknown LLM action: %s", agent_id, action)
        return {"action": "unknown", "detail": str(response)}

    except Exception as exc:
        logger.error("[%s] Decision loop error: %s", agent_id, exc, exc_info=True)
        return {"action": "error", "detail": str(exc)}


def _get_balance(conn, agent_id: str) -> float:
    from store.db import get_latest_account
    latest = get_latest_account(conn, agent_id, "paper")
    return latest["balance"] if latest else 50000.0
