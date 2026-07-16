"""
agents/decision_loop.py — Core decision pipeline for one agent wake cycle.

run_decision never raises. All exceptions are caught, logged, and returned
as {"action": "error", "detail": str(exc)}.
"""

import json
import logging

from agents.persona import build_system_prompt
from agents.prompt_builder import build_decision_prompt, build_portfolio_snapshot
from market.heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    heartbeat_max_age_seconds,
    read_heartbeat_or_none,
)
from risk.gate import RiskViolation, validate_order
from store.db import get_positions, update_last_model_used
from store.positions import has_open_position_for_asset
from store.fingerprint import write_entry, write_outcome

logger = logging.getLogger(__name__)


def build_trade_market_context(
    heartbeat: dict, asset: str, conn, agent_id: str, config: dict
) -> dict:
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
        heartbeat = read_heartbeat_or_none(
            heartbeat_path, heartbeat_max_age_seconds(config)
        )
        if heartbeat is None:
            log_decision(conn, agent_id, "wait", "heartbeat unavailable or stale", None)
            return {"action": "wait", "detail": "heartbeat unavailable or stale"}

        # Check if this agent is a benchmark agent (random_walk or btc_hold)
        import json as _json
        agent_row = conn.execute(
            "SELECT config_json FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        response = None
        model_used = None
        system_prompt = ""
        if agent_row:
            cfg = _json.loads(agent_row["config_json"])
            benchmark_type = cfg.get("benchmark_type")
            if benchmark_type:
                import random as _random
                from store.db import _now as _db_now

                if benchmark_type == "random_walk":
                    action = _random.choices(
                        ["enter", "wait", "close"],
                        weights=[0.3, 0.6, 0.1],
                        k=1,
                    )[0]
                    if action == "enter":
                        asset = _random.choice(config["universe"])
                        entry_price = (heartbeat.get("assets") or {}).get(asset, {}).get("price", 100.0)
                        direction = _random.choice(["long", "short"])
                        if direction == "long":
                            sl = entry_price * 0.98
                            tp = entry_price * 1.04
                        else:
                            sl = entry_price * 1.02
                            tp = entry_price * 0.96
                        response = {
                            "action": "enter",
                            "asset": asset,
                            "direction": direction,
                            "entry_price": entry_price,
                            "stop_loss_price": sl,
                            "take_profit_price": tp,
                            "leverage": 3,
                            "position_size_pct": 0.10,
                            "confidence": 0.5,
                        }
                    elif action == "close":
                        positions = get_positions(conn, agent_id)
                        if positions:
                            pos = _random.choice(positions)
                            response = {
                                "action": "close",
                                "position_id": pos["id"],
                                "reason": "benchmark_close",
                            }
                        else:
                            response = {"action": "wait", "reason": "no positions to close"}
                    else:
                        response = {"action": "wait", "reason": "benchmark_wait"}

                    model_used = "benchmark_random_walk"

                elif benchmark_type == "btc_hold":
                    asset = "BTC-PERP"
                    entry_price = (heartbeat.get("assets") or {}).get(asset, {}).get("price", 100.0)
                    if has_open_position_for_asset(conn, agent_id, asset):
                        # Buy-and-hold: the benchmark establishes its position
                        # once and holds — it must not stack a new long every
                        # wake cycle (R2 AC#4).
                        response = {"action": "wait", "reason": "benchmark holding BTC"}
                    elif entry_price and entry_price > 0:
                        response = {
                            "action": "enter",
                            "asset": asset,
                            "direction": "long",
                            "entry_price": entry_price,
                            "stop_loss_price": entry_price * 0.90,
                            "take_profit_price": entry_price * 5.0,
                            "leverage": 1,
                            "position_size_pct": 0.10,
                            "confidence": 1.0,
                            # Hold means hold: without this, the bridge's 48h
                            # default max_hold churns the benchmark (close +
                            # re-enter + fees) every two days.
                            "max_hold_hours": 24.0 * 365,
                        }
                    else:
                        response = {"action": "wait", "reason": "no BTC price"}
                    model_used = "benchmark_btc_hold"

                update_last_model_used(conn, agent_id, model_used)

        # --- M8: Compiled agent support ------------------------------------
        if agent_row and response is None:
            cfg = _json.loads(agent_row["config_json"])
            if cfg.get("compiled", False):
                from store.specs import get_active_spec
                from backtest.interpreter import evaluate

                active_spec = get_active_spec(conn, agent_id)
                if active_spec is None:
                    update_last_model_used(conn, agent_id, "compiled/none")
                    log_decision(
                        conn, agent_id, "wait", "compiled: no active spec deployed", None,
                        model_used="compiled/none",
                    )
                    return {"action": "wait", "detail": "compiled: no active spec deployed"}

                assets = heartbeat.get("assets", {})
                best_asset = None
                best_decision = None
                best_confidence = 0.0

                for asset_name, asset_fields in assets.items():
                    feature_row = dict(asset_fields)
                    decision = evaluate(active_spec, feature_row)

                    if decision["confidence"] > best_confidence:
                        best_confidence = decision["confidence"]
                        best_asset = asset_name
                        best_decision = decision

                    if decision["action"] == "enter" and decision["confidence"] >= active_spec.confidence_threshold:
                        break

                # --- M10: Shadow-challenger evaluation ------------------
                from store.specs import get_challenger_spec

                challenger_spec = get_challenger_spec(conn, agent_id)
                if challenger_spec is not None:
                    ch_best_asset = None
                    ch_best_decision = None
                    ch_best_confidence = 0.0

                    for asset_name, asset_fields in assets.items():
                        ch_row = dict(asset_fields)
                        ch_decision = evaluate(challenger_spec, ch_row)

                        if ch_decision["confidence"] > ch_best_confidence:
                            ch_best_confidence = ch_decision["confidence"]
                            ch_best_asset = asset_name
                            ch_best_decision = ch_decision

                        if (
                            ch_decision["action"] == "enter"
                            and ch_decision["confidence"]
                            >= challenger_spec.confidence_threshold
                        ):
                            break

                    if ch_best_decision is not None:
                        ch_details: dict = {
                            "challenger_spec_version": challenger_spec.spec_version,
                            "challenger_confidence": ch_best_decision["confidence"],
                            "challenger_action": ch_best_decision["action"],
                            "challenger_asset": ch_best_asset,
                            "challenger_evidence_strength": ch_best_decision.get(
                                "evidence_strength",
                            ),
                            "incumbent_spec_version": active_spec.spec_version,
                        }
                        log_decision(
                            conn,
                            agent_id,
                            ch_best_decision["action"],
                            f"challenger/v{challenger_spec.spec_version}"
                            f" shadow on {ch_best_asset}",
                            ch_details,
                            confidence=ch_best_decision["confidence"],
                            evidence_strength=ch_best_decision.get("evidence_strength"),
                            model_used=f"compiled/v{challenger_spec.spec_version}",
                        )

                if best_decision:
                    if has_open_position_for_asset(conn, agent_id, best_asset):
                        update_last_model_used(
                            conn, agent_id, f"compiled/v{active_spec.spec_version}"
                        )
                        log_decision(
                            conn, agent_id, "wait",
                            f"compiled: already holds {best_asset}",
                            None,
                            confidence=best_decision["confidence"],
                            evidence_strength=best_decision.get("evidence_strength"),
                            model_used=f"compiled/v{active_spec.spec_version}",
                        )
                        return {"action": "wait", "detail": f"already holds {best_asset}"}

                    price = assets.get(best_asset, {}).get("price", 0)
                    response = {
                        "action": "enter",
                        "asset": best_asset,
                        "direction": active_spec.direction,
                        "entry_price": price,
                        "stop_loss_price": (
                            price * (1 - active_spec.stop_loss_pct)
                            if active_spec.direction == "long"
                            else price * (1 + active_spec.stop_loss_pct)
                        ),
                        "take_profit_price": (
                            price * (1 + active_spec.take_profit_pct)
                            if active_spec.direction == "long"
                            else price * (1 - active_spec.take_profit_pct)
                        ),
                        "leverage": active_spec.leverage,
                        "position_size_pct": active_spec.position_size_pct,
                        "confidence": best_decision["confidence"],
                        # Spec thresholds ride along so the shared sizing
                        # formula in the enter branch scales exactly as the
                        # backtest engine does for this spec.
                        "confidence_threshold": active_spec.confidence_threshold,
                        "scale_threshold": active_spec.scale_threshold,
                        "evidence_strength": best_decision.get("evidence_strength", {}),
                        "max_hold_hours": getattr(active_spec, "max_hold_hours", 48),
                    }
                    model_used = f"compiled/v{active_spec.spec_version}"
                    update_last_model_used(conn, agent_id, model_used)
                else:
                    candidate_info = None
                    if best_decision and best_asset:
                        price = assets.get(best_asset, {}).get("price", 0)
                        if price and price > 0:
                            sl_price = (
                                price * (1 - active_spec.stop_loss_pct)
                                if active_spec.direction == "long"
                                else price * (1 + active_spec.stop_loss_pct)
                            )
                            tp_price = (
                                price * (1 + active_spec.take_profit_pct)
                                if active_spec.direction == "long"
                                else price * (1 - active_spec.take_profit_pct)
                            )
                            candidate_info = {
                                "candidate": {
                                    "asset": best_asset,
                                    "direction": active_spec.direction,
                                    "entry_price": price,
                                    "stop_loss_price": sl_price,
                                    "take_profit_price": tp_price,
                                    "confidence": best_decision["confidence"],
                                    "max_hold_hours": getattr(active_spec, "max_hold_hours", 48),
                                }
                            }
                    update_last_model_used(
                        conn, agent_id, f"compiled/v{active_spec.spec_version}"
                    )
                    log_decision(
                        conn, agent_id, "wait",
                        f"compiled: no asset met threshold (best={best_confidence:.2f})",
                        candidate_info,
                        confidence=best_decision["confidence"] if best_decision else 0.0,
                        evidence_strength=best_decision.get("evidence_strength") if best_decision else {},
                        model_used=f"compiled/v{active_spec.spec_version}",
                    )
                    return {"action": "wait", "detail": "compiled: no asset met threshold"}

        if response is None:
            system_prompt = build_system_prompt(agent_id, config)
            decision_prompt = await build_decision_prompt(
                agent_id,
                thesis_text,
                heartbeat,
                conn,
                provider,
                starting_balance=desk_config["starting_balance"],
                universe=assets,
            )

            response, model_used = _call_llm_with_retry(
                llm_fn, system_prompt, decision_prompt, agent_id
            )

            # Record which model produced (or failed to produce) this cycle's
            # decision — "most recently used model", not "model used for the
            # last trade": every cycle updates this, including wait/error
            # cycles, per the captain's requirement. The literal
            # "no model available" sentinel (distinct from NULL, which means
            # "no decision cycle has recorded a model yet" e.g. legacy rows)
            # covers the case where model_chain.decide() exhausted every tier.
            model_label = model_used
            if (
                model_label is None
                and response is not None
                and response.get("action") == "error"
            ):
                model_label = "no model available"
            if model_label is not None:
                update_last_model_used(conn, agent_id, model_label)

            if response is None:
                log_decision(conn, agent_id, "wait", "LLM returned invalid response after retries", None)
                return {
                    "action": "wait",
                    "detail": "LLM returned invalid response after retries",
                }
        else:
            model_label = model_used

        action = response.get("action", "wait")

        if action == "error":
            reason = response.get("reason", "no model available")
            logger.error("[%s] No model available for decision: %s", agent_id, reason)
            log_decision(conn, agent_id, "error", reason, None, model_used=model_label)
            return {"action": "error", "detail": reason}

        if action == "wait":
            reason = response.get("reason", "")
            logger.info("[%s] LLM decided to wait: %s", agent_id, reason)
            # R3-capture: persist the candidate the agent came closest to
            # taking, in the exact shape store/counterfactuals.py replays.
            # Without this, wait decisions can never be counterfactually
            # scored — the replay engine skips detail-less waits.
            candidate = _extract_wait_candidate(response, heartbeat)
            log_decision(
                conn, agent_id, "wait", reason,
                {"candidate": candidate} if candidate else None,
                confidence=response.get("confidence"),
                evidence_strength=response.get("evidence_strength"),
                model_used=model_label,
            )
            return {"action": "wait", "detail": reason}

        if action == "close":
            pos_id = response.get("position_id")
            reason = response.get("reason", "agent_close")
            bridge = bridge_factory(agent_id, conn, provider)
            fill = await bridge.close(pos_id, reason)
            logger.info("[%s] Closed position %s: %s", agent_id, pos_id, fill)
            trade_id = fill.get("trade_id")
            log_decision(
                conn, agent_id, "close", reason,
                {"position_id": pos_id, "fill": str(fill)},
                model_used=model_label,
            )
            if trade_id:
                try:
                    await run_postmortem(conn, agent_id, trade_id, llm_fn, system_prompt)
                except Exception as exc:
                    logger.warning(
                        "[%s] Postmortem failed for trade %s: %s", agent_id, trade_id, exc
                    )
            return {"action": "close", "detail": str(fill)}

        if action == "enter":
            # Confidence sizing happens BEFORE the risk gate so the gate
            # validates the order that actually executes (R4 AC#4).  The
            # formula is the shared execution/sizing.py one — identical to
            # backtest/engine.py's; compiled responses carry their spec's
            # thresholds, LLM responses use the thesis defaults (0.70/0.50).
            from execution.sizing import (
                DEFAULT_CONFIDENCE_THRESHOLD,
                DEFAULT_SCALE_THRESHOLD,
                scale_position_size,
            )

            response["position_size_pct"] = scale_position_size(
                response["position_size_pct"],
                response.get("confidence"),
                confidence_threshold=response.get(
                    "confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD
                ),
                scale_threshold=response.get(
                    "scale_threshold", DEFAULT_SCALE_THRESHOLD
                ),
            )

            # M9 crit 7: the risk officer's entry gate is enforced here —
            # per-agent disables (gross-exposure throttle, human stops),
            # the desk-wide event blackout, and the kill switch all block
            # NEW entries. Closes/SL-TP handling never pass through this
            # branch, so risk reduction is never blocked.
            from meta.risk_officer import RiskOfficer

            gate_open, gate_reason = RiskOfficer(conn, config).entry_gate_status(agent_id)
            if not gate_open:
                logger.warning(
                    "[%s] Risk officer blocked entry: %s", agent_id, gate_reason
                )
                log_decision(
                    conn, agent_id, "risk_blocked",
                    f"risk officer blocked entry: {gate_reason}",
                    {"risk_reason": f"risk officer: {gate_reason}", "order": str(response)},
                    confidence=response.get("confidence"),
                    evidence_strength=response.get("evidence_strength"),
                    model_used=model_label,
                )
                return {"action": "risk_blocked", "detail": f"risk officer: {gate_reason}"}

            open_positions = get_positions(conn, agent_id)
            asset_data = (heartbeat.get("assets") or {}).get(response.get("asset", ""))
            heartbeat_price = (asset_data or {}).get("price") if asset_data else None
            try:
                validate_order(
                    order=response,
                    account_balance=_get_balance(
                        conn, agent_id, desk_config["starting_balance"]
                    ),
                    config=desk_config,
                    open_position_count=len(open_positions),
                    heartbeat_price=heartbeat_price,
                )
            except RiskViolation as e:
                logger.warning("[%s] Risk gate blocked order: %s", agent_id, e.reason)
                log_decision(
                    conn, agent_id, "risk_blocked", f"risk gate blocked: {e.reason}",
                    {"risk_reason": e.reason, "order": str(response)},
                    confidence=response.get("confidence"),
                    evidence_strength=response.get("evidence_strength"),
                    model_used=model_label,
                )
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
                    "open_interest_24h_change_pct": asset_fields.get("oi_zscore", 0)
                    or 0,
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
            log_decision(
                conn, agent_id, "enter", f"entered {response['asset']}",
                {"order": str(response), "fill": str(fill)},
                confidence=response.get("confidence"),
                evidence_strength=response.get("evidence_strength"),
                model_used=model_label,
            )
            return {"action": "enter", "detail": str(fill)}

        logger.warning(
            "[%s] Unrecognized LLM action '%s', treating as wait", agent_id, action
        )
        log_decision(
            conn, agent_id, "wait", f"unrecognized LLM action: {action}", None,
            model_used=model_label,
        )
        return {"action": "wait", "detail": f"unrecognized LLM action: {action}"}

    except Exception as exc:
        logger.error("[%s] Decision loop error: %s", agent_id, exc, exc_info=True)
        return {"action": "error", "detail": str(exc)}


def _call_llm_with_retry(
    llm_fn,
    system_prompt: str,
    decision_prompt: str,
    agent_id: str | None,
    max_retries: int = 2,
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
            result = llm_fn(system_prompt, decision_prompt, agent_id=agent_id)
        except Exception as exc:
            logger.warning(
                "LLM call failed (attempt %d/%d): %s", attempt + 1, max_retries, exc
            )
            if attempt < max_retries:
                continue
            return None, last_model_used

        decision, model_used = result if isinstance(result, tuple) else (result, None)
        if model_used:
            last_model_used = model_used

        if not isinstance(decision, dict):
            logger.warning(
                "LLM returned non-dict (attempt %d/%d)", attempt + 1, max_retries
            )
            if attempt < max_retries:
                decision_prompt += "\n\nYour previous response was not valid JSON. Output a valid JSON object only."
                continue
            return None, last_model_used

        action = decision.get("action")

        if action == "error":
            return decision, model_used

        if action not in ("enter", "wait", "close"):
            logger.warning(
                "LLM returned unknown action %r (attempt %d/%d)",
                action,
                attempt + 1,
                max_retries,
            )
            if attempt < max_retries:
                decision_prompt += f"\n\nAction '{action}' is not valid. Use 'enter', 'wait', or 'close'."
                continue
            return None, last_model_used

        if action == "enter":
            required = (
                "asset",
                "direction",
                "entry_price",
                "stop_loss_price",
                "leverage",
                "position_size_pct",
                "confidence",
            )
            missing = [k for k in required if k not in decision]
            if missing:
                logger.warning(
                    "LLM enter missing fields %s (attempt %d/%d)",
                    missing,
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries:
                    decision_prompt += f"\n\nMissing required fields: {missing}. Include all trade parameters."
                    continue
                return None, last_model_used

        return decision, model_used

    return None, last_model_used


async def run_postmortem(
    conn, agent_id: str, trade_id: str, llm_fn, system_prompt: str
) -> None:
    """Generate a one-sentence postmortem for a just-closed trade."""
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return
    trade = dict(row)
    try:
        prompt = (
            f"Write one sentence analyzing why this trade {'won' if trade.get('pnl_pct', 0) > 0 else 'lost'}. "
            f"Asset: {trade['asset']}, Direction: {trade['direction']}, "
            f"PnL: {trade.get('pnl_pct', 0):+.2%}, "
            f"Exit reason: {trade.get('exit_reason', '?')}. "
            f"Entry thesis: {(trade.get('hypothesis') or 'N/A')[:200]}"
        )
        result = llm_fn(system_prompt, prompt, agent_id=agent_id)
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


def _extract_wait_candidate(response: dict, heartbeat: dict) -> dict | None:
    """Validate and normalise the candidate block of a wait decision.

    Returns a dict in the exact shape store/counterfactuals.py replays
    ({asset, direction, entry_price, stop_loss_price, take_profit_price},
    plus optional confidence/max_hold_hours), or None when the response
    carries nothing replayable — a half-filled candidate is never guessed
    into shape.
    """
    candidate = response.get("candidate")
    if not isinstance(candidate, dict):
        return None

    asset = candidate.get("asset")
    direction = candidate.get("direction")
    if not asset or direction not in ("long", "short"):
        return None

    entry_price = candidate.get("entry_price")
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        # The heartbeat price at decision time is the honest fill-in — it is
        # the price the agent was looking at when it declined the trade.
        entry_price = ((heartbeat.get("assets") or {}).get(asset) or {}).get("price")
        if not isinstance(entry_price, (int, float)) or entry_price <= 0:
            return None

    sl = candidate.get("stop_loss_price")
    tp = candidate.get("take_profit_price")
    if not isinstance(sl, (int, float)) or not isinstance(tp, (int, float)):
        return None

    out = {
        "asset": asset,
        "direction": direction,
        "entry_price": float(entry_price),
        "stop_loss_price": float(sl),
        "take_profit_price": float(tp),
    }
    if isinstance(candidate.get("confidence"), (int, float)):
        out["confidence"] = float(candidate["confidence"])
    elif isinstance(response.get("confidence"), (int, float)):
        out["confidence"] = float(response["confidence"])
    if isinstance(candidate.get("max_hold_hours"), (int, float)):
        out["max_hold_hours"] = float(candidate["max_hold_hours"])
    return out


def _get_balance(conn, agent_id: str, starting_balance: float) -> float:
    from store.db import get_latest_account

    latest = get_latest_account(conn, agent_id, "paper")
    return latest["balance"] if latest else starting_balance


def log_decision(
    conn,
    agent_id: str,
    action: str,
    reason: str | None,
    details: dict | None,
    confidence: float | None = None,
    evidence_strength: dict | None = None,
    model_used: str | None = None,
) -> None:
    """Log a decision to the decisions table AND the git-tracked ledger.

    confidence/evidence_strength/model_used are what the calibration goal
    depends on -- every cycle for every agent, wait included, not just
    enter. See docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
    """
    from store.db import _now
    from store.ledger import append_ledger_record

    timestamp = _now()
    conn.execute(
        """INSERT INTO decisions (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, timestamp, action, reason, json.dumps(details) if details else None),
    )
    conn.commit()

    append_ledger_record(
        "decisions",
        {
            "ts": timestamp,
            "agent": agent_id,
            "action": action,
            "reason": reason,
            "confidence": confidence,
            "evidence_strength": evidence_strength,
            "model": model_used,
        },
    )


async def run_counterfactual(
    conn, agent_id: str, trade_id: str, llm_fn, system_prompt: str
) -> None:
    """Run a counterfactual analysis for a 'wait' decision that could have been a trade.

    This is called nightly to analyze past wait decisions and determine if taking
    the trade would have been profitable.
    """
    # Get the last wait decision for this agent
    row = conn.execute(
        """SELECT d.*, t.asset, t.entry_price, t.stop_loss_price, t.take_profit_price
           FROM decisions d
           LEFT JOIN trades t ON d.agent_id = t.agent_id
           WHERE d.agent_id = ? AND d.decision_action = 'wait'
           ORDER BY d.timestamp DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()

    if not row:
        return

    decision = dict(row)
    if not decision.get("asset"):
        return

    # Build a prompt asking the LLM to analyze what would have happened
    prompt = (
        f"Counterfactual analysis for agent {agent_id}:\n"
        f"Asset: {decision['asset']}\n"
        f"Decision at {decision['timestamp']}: wait\n"
        f"Current price: {decision.get('entry_price', 'N/A')}\n"
        f"SL: {decision.get('stop_loss_price', 'N/A')}\n"
        f"TP: {decision.get('take_profit_price', 'N/A')}\n"
        f"\nBased on the market context at that time, would taking a long or short position "
        f"have been profitable? What would the PnL have been?\n"
        f'Respond with JSON: {{"action": "long"|"short"|"wait", "expected_pnl_pct": number, "confidence": number}}'
    )

    try:
        result = llm_fn(system_prompt, prompt, agent_id=agent_id)
        if isinstance(result, tuple):
            result, _ = result
        if isinstance(result, dict):
            # Store the counterfactual result
            conn.execute(
                """UPDATE decisions 
                   SET counterfactual_result = ?, counterfactual_was_better = ?
                   WHERE id = ?""",
                (
                    json.dumps(result),
                    1 if result.get("action") in ("long", "short") else 0,
                    decision["id"],
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "[%s] Counterfactual analysis failed for %s: %s",
            agent_id,
            decision["id"],
            exc,
        )
