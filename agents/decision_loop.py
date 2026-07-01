"""
agents/decision_loop.py — Core decision pipeline for one agent wake cycle.

run_decision never raises. All exceptions are caught, logged, and returned
as {"action": "error", "detail": str(exc)}.
"""

import asyncio
import logging
from agents.persona import build_system_prompt
from agents.prompt_builder import build_decision_prompt, build_portfolio_snapshot
from market.heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    heartbeat_max_age_seconds,
    read_heartbeat_or_none,
)
from risk.gate import validate_order, RiskViolation
from store.db import get_positions, get_trades, insert_trade, update_last_model_used
from store.fingerprint import write_entry, write_outcome

logger = logging.getLogger(__name__)


def build_trade_market_context(heartbeat: dict, asset: str, conn, agent_id: str, config: dict) -> dict:
    """Consolidate the full trade-entry context — the trade "thumbprint" the
    captain asked for — into one dict for write_entry()'s `market_context`
    param:

      - portfolio: this agent's cash/equity/exposure/open-positions/PnL/
        risk-utilization at the moment of the trade, via
        build_portfolio_snapshot() (the same helper the decision prompt's
        Portfolio section is built from — not recomputed differently here).
      - cross_asset / regime: the heartbeat's non-asset-specific blocks,
        as-is.
      - asset: the FULL per-asset heartbeat field dict for the traded
        asset (all ~29 fields, including candles_5m/candles_30m/candles_4h).

    Recorded so other agents' cross-agent trade-bank queries and the web UI
    can see exactly what this agent saw at entry, not just a narrow
    funding/OI proxy.
    """
    return {
        "portfolio": build_portfolio_snapshot(conn, agent_id, config),
        "cross_asset": heartbeat.get("cross_asset") or {},
        "regime": heartbeat.get("regime") or {},
        "asset": (heartbeat.get("assets") or {}).get(asset) or {},
    }


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

        heartbeat_path = desk_config.get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
        heartbeat = read_heartbeat_or_none(heartbeat_path, heartbeat_max_age_seconds(config))
        if heartbeat is None:
            return {"action": "wait", "detail": "heartbeat unavailable or stale"}

        system_prompt = build_system_prompt(agent_id, config)
        decision_prompt = await build_decision_prompt(
            agent_id, thesis_text, heartbeat, conn, provider,
            starting_balance=desk_config["starting_balance"],
            universe=assets,
        )

        response, model_used = _call_llm_with_retry(llm_fn, system_prompt, decision_prompt)

        # Record which model produced (or failed to produce) this cycle's
        # decision — "most recently used model", not "model used for the
        # last trade": every cycle updates this, including wait/error
        # cycles, per the captain's requirement. The literal
        # "no model available" sentinel (distinct from NULL, which means
        # "no decision cycle has recorded a model yet" e.g. legacy rows)
        # covers the case where model_chain.decide() exhausted every tier.
        model_label = model_used
        if model_label is None and response is not None and response.get("action") == "error":
            model_label = "no model available"
        if model_label is not None:
            update_last_model_used(conn, agent_id, model_label)

        if response is None:
            return {"action": "wait", "detail": "LLM returned invalid response after retries"}

        action = response.get("action", "wait")

        if action == "error":
            reason = response.get("reason", "no model available")
            logger.error("[%s] No model available for decision: %s", agent_id, reason)
            return {"action": "error", "detail": reason}

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

            # Write the fingerprint onto the trade row the bridge just
            # created: the consolidated trade-thumbprint (portfolio state +
            # heartbeat's cross_asset/regime blocks + the full per-asset
            # heartbeat fields for the traded asset, including OHLCV
            # candles) alongside the agent's reasoning and the categorical
            # regime tag. See docs/superpowers/specs/
            # 2026-07-01-heartbeat-wiring-design.md for the shape.
            if fill.get("trade_id"):
                market_context = build_trade_market_context(
                    heartbeat, response["asset"], conn, agent_id, config
                )
                asset_fields = market_context["asset"]
                asset_snapshot = {
                    "funding_rate_current": asset_fields.get("funding", 0) or 0,
                    "open_interest_24h_change_pct": asset_fields.get("oi_zscore", 0) or 0,
                }
                write_entry(
                    conn,
                    fill["trade_id"],
                    asset_snapshot,
                    regime=(heartbeat.get("regime") or {}).get("regime_tag"),
                    reasoning=response,
                    market_context=market_context,
                    model_used=model_used,
                )

            logger.info("[%s] Entered trade: %s", agent_id, fill)
            return {"action": "enter", "detail": str(fill)}

        logger.warning("[%s] Unrecognized LLM action '%s', treating as wait", agent_id, action)
        return {"action": "wait", "detail": f"unrecognized LLM action: {action}"}

    except Exception as exc:
        logger.error("[%s] Decision loop error: %s", agent_id, exc, exc_info=True)
        return {"action": "error", "detail": str(exc)}


def _call_llm_with_retry(
    llm_fn, system_prompt: str, decision_prompt: str, max_retries: int = 2
) -> tuple[dict | None, str | None]:
    """Call LLM and validate JSON response. Retry up to max_retries on bad output.

    `llm_fn` returns `(decision_dict, model_display_name_or_None)` — see
    llm/model_chain.py's `decide()`. Returns the same shape: on success,
    `(decision, model_used)` for the accepted decision; on exhaustion,
    `(None, last_model_used)` where `last_model_used` is whichever model
    (if any) answered on the final attempt, so callers can still record
    which model was involved even when every attempt was ultimately
    rejected as malformed.

    A decision with `action == "error"` (the model chain's own explicit
    "no model available" signal) is returned immediately without
    retry-reprompting — reprompting only makes sense for malformed/
    incomplete JSON that a clarifying follow-up message could fix.
    """
    last_model_used = None
    for attempt in range(max_retries + 1):
        try:
            result = llm_fn(system_prompt, decision_prompt)
        except Exception as exc:
            logger.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            if attempt < max_retries:
                continue
            return None, last_model_used

        decision, model_used = result if isinstance(result, tuple) else (result, None)
        if model_used:
            last_model_used = model_used

        if not isinstance(decision, dict):
            logger.warning("LLM returned non-dict (attempt %d/%d)", attempt + 1, max_retries)
            if attempt < max_retries:
                decision_prompt += "\n\nYour previous response was not valid JSON. Output a valid JSON object only."
                continue
            return None, last_model_used

        action = decision.get("action")

        if action == "error":
            return decision, model_used

        if action not in ("enter", "wait", "close"):
            logger.warning("LLM returned unknown action %r (attempt %d/%d)", action, attempt + 1, max_retries)
            if attempt < max_retries:
                decision_prompt += f"\n\nAction '{action}' is not valid. Use 'enter', 'wait', or 'close'."
                continue
            return None, last_model_used

        if action == "enter":
            required = ("asset", "direction", "entry_price", "stop_loss_price", "leverage", "position_size_pct")
            missing = [k for k in required if k not in decision]
            if missing:
                logger.warning("LLM enter missing fields %s (attempt %d/%d)", missing, attempt + 1, max_retries)
                if attempt < max_retries:
                    decision_prompt += f"\n\nMissing required fields: {missing}. Include all trade parameters."
                    continue
                return None, last_model_used

        return decision, model_used

    return None, last_model_used


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
        # llm_fn now returns (decision_dict, model_used) — see
        # llm/model_chain.py's decide(). The model label isn't tracked
        # per-postmortem (out of scope), only the decision dict matters here.
        if isinstance(result, tuple):
            result, _ = result
        if isinstance(result, dict) and result.get("action") == "wait":
            postmortem = result.get("reason", "")
        elif isinstance(result, str):
            postmortem = result
        else:
            postmortem = str(result) if result else ""
        if postmortem:
            write_outcome(conn, trade_id, {"agent_postmortem": postmortem.strip()})
    except Exception as exc:
        logger.warning("[%s] Postmortem failed for %s: %s", agent_id, trade_id, exc)


def _get_balance(conn, agent_id: str, starting_balance: float) -> float:
    from store.db import get_latest_account
    latest = get_latest_account(conn, agent_id, "paper")
    return latest["balance"] if latest else starting_balance
