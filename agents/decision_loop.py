"""
agents/decision_loop.py — Core decision pipeline for one agent wake cycle.

run_decision never raises. All exceptions are caught, logged, and returned
as {"action": "error", "detail": str(exc)}.
"""

import asyncio
import json
import logging
from agents.persona import build_system_prompt
from agents.prompt_builder import build_decision_prompt
from risk.gate import validate_order, RiskViolation
from store.db import get_positions, get_trades, insert_trade

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

        response = _call_llm_with_retry(llm_fn, system_prompt, decision_prompt)
        if response is None:
            return {"action": "wait", "detail": "LLM returned invalid response after retries"}

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
            trade_id = fill.get("trade_id")
            if trade_id:
                asyncio.ensure_future(
                    run_postmortem(conn, agent_id, trade_id, llm_fn, system_prompt)
                )
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

            # Write reasoning fields to the trade record
            if fill.get("trade_id"):
                conn.execute(
                    """UPDATE trades SET hypothesis=?,
                       key_conditions_met=?, key_conditions_missing=?,
                       confidence=?, expected_value=? WHERE id=?""",
                    (
                        response.get("hypothesis", ""),
                        json.dumps(response.get("key_conditions_met", [])),
                        json.dumps(response.get("key_conditions_missing", [])),
                        response.get("confidence", 0.0),
                        response.get("expected_value", ""),
                        fill["trade_id"],
                    ),
                )
                conn.commit()

            logger.info("[%s] Entered trade: %s", agent_id, fill)
            return {"action": "enter", "detail": str(fill)}

        logger.warning("[%s] Unrecognized LLM action '%s', treating as wait", agent_id, action)
        return {"action": "wait", "detail": f"unrecognized LLM action: {action}"}

    except Exception as exc:
        logger.error("[%s] Decision loop error: %s", agent_id, exc, exc_info=True)
        return {"action": "error", "detail": str(exc)}


def _call_llm_with_retry(llm_fn, system_prompt: str, decision_prompt: str, max_retries: int = 2) -> dict | None:
    """Call LLM and validate JSON response. Retry up to max_retries on bad output."""
    for attempt in range(max_retries + 1):
        try:
            result = llm_fn(system_prompt, decision_prompt)
        except Exception as exc:
            logger.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            if attempt < max_retries:
                continue
            return None

        if not isinstance(result, dict):
            logger.warning("LLM returned non-dict (attempt %d/%d)", attempt + 1, max_retries)
            if attempt < max_retries:
                decision_prompt += "\n\nYour previous response was not valid JSON. Output a valid JSON object only."
                continue
            return None

        action = result.get("action")
        if action not in ("enter", "wait", "close"):
            logger.warning("LLM returned unknown action %r (attempt %d/%d)", action, attempt + 1, max_retries)
            if attempt < max_retries:
                decision_prompt += f"\n\nAction '{action}' is not valid. Use 'enter', 'wait', or 'close'."
                continue
            return None

        if action == "enter":
            required = ("asset", "direction", "entry_price", "stop_loss_price", "leverage", "position_size_pct")
            missing = [k for k in required if k not in result]
            if missing:
                logger.warning("LLM enter missing fields %s (attempt %d/%d)", missing, attempt + 1, max_retries)
                if attempt < max_retries:
                    decision_prompt += f"\n\nMissing required fields: {missing}. Include all trade parameters."
                    continue
                return None

        return result

    return None


async def run_postmortem(conn, agent_id: str, trade_id: str, llm_fn, system_prompt: str) -> None:
    """Generate a one-sentence postmortem for a just-closed trade."""
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return
    trade = dict(row)
    prompt = (
        f"Write one sentence analyzing why this trade {'won' if trade.get('pnl_pct', 0) > 0 else 'lost'}. "
        f"Asset: {trade['asset']}, Direction: {trade['direction']}, "
        f"PnL: {trade.get('pnl_pct', 0):+.2%}, "
        f"Exit reason: {trade.get('exit_reason', '?')}. "
        f"Entry thesis: {trade.get('hypothesis', 'N/A')[:200]}"
    )
    try:
        result = llm_fn(system_prompt, prompt)
        if isinstance(result, dict) and result.get("action") == "wait":
            postmortem = result.get("reason", "")
        elif isinstance(result, str):
            postmortem = result
        else:
            postmortem = str(result) if result else ""
        if postmortem:
            conn.execute("UPDATE trades SET agent_postmortem=? WHERE id=?", (postmortem.strip(), trade_id))
            conn.commit()
    except Exception as exc:
        logger.warning("[%s] Postmortem failed for %s: %s", agent_id, trade_id, exc)


def _get_balance(conn, agent_id: str, starting_balance: float) -> float:
    from store.db import get_latest_account
    latest = get_latest_account(conn, agent_id, "paper")
    return latest["balance"] if latest else starting_balance
