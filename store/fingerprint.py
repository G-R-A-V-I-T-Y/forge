"""
store/fingerprint.py — write the full trade fingerprint (entry + outcome).

The execution bridge (e.g. PaperBridge.enter) is responsible for the
authoritative trade row: id, prices, size, status. This module enriches
that row with the institutional-memory payload described in the Trade
Fingerprint Schema: the market snapshot at entry (OHLCV compressed with
msgpack, funding/OI/liquidation context, regime tag) and the agent's
reasoning, then later attaches the outcome/postmortem at close.

write_entry() and write_outcome() both operate on an *existing* trade row
(already INSERTed by the bridge) via UPDATE — this avoids a second INSERT
racing the bridge's own write and keeps the bridge solely responsible for
account/position bookkeeping.
"""
import json
import logging

import msgpack

logger = logging.getLogger(__name__)

# Per-asset market_state keys captured into dedicated, queryable columns.
# Everything else in the market snapshot dict (mid_price, bid, ask, btc
# dominance, correlations, etc.) is preserved in market_context_json.
_OHLCV_KEYS = {"ohlcv_15m": "ohlcv_15m_blob", "ohlcv_1h": "ohlcv_1h_blob", "ohlcv_4h": "ohlcv_4h_blob"}
_DEDICATED_KEYS = {
    "funding_rate_current",
    "funding_rate_8h_history",
    "open_interest_usd",
    "open_interest_24h_change_pct",
    "liquidation_volume_1h_usd",
    "liquidation_direction_dominant",
}
_REASONING_KEYS = ("hypothesis", "key_conditions_met", "key_conditions_missing",
                    "confidence", "expected_value")
_OUTCOME_KEYS = ("exit_price", "exit_timestamp", "exit_reason", "duration_minutes",
                  "pnl_pct", "pnl_usd", "result", "agent_postmortem")


def pack_ohlcv(candles: list[list] | None) -> bytes:
    """Compress an OHLCV candle array with msgpack for compact SQLite storage."""
    return msgpack.packb(candles or [], use_bin_type=True)


def unpack_ohlcv(blob: bytes | None) -> list[list]:
    """Decompress a msgpack OHLCV blob back into a list of candles."""
    if not blob:
        return []
    return msgpack.unpackb(blob, raw=False)


def write_entry(conn, trade_id: str, market_snapshot: dict, regime: str | None = None,
                 reasoning: dict | None = None) -> None:
    """Enrich an already-inserted trade row with the full entry fingerprint.

    Parameters
    ----------
    trade_id:
        id of the trade row created by the execution bridge.
    market_snapshot:
        Per-asset dict as returned by MarketProvider.get_market_state()[asset]
        — i.e. one value of that dict, not the whole multi-asset state.
    regime:
        Market regime tag at entry time (e.g. market_state["_regime"]).
    reasoning:
        The agent's decision payload: hypothesis, key_conditions_met,
        key_conditions_missing, confidence, expected_value.
    """
    market_snapshot = market_snapshot or {}
    fields: dict = {}

    for state_key, col in _OHLCV_KEYS.items():
        fields[col] = pack_ohlcv(market_snapshot.get(state_key))

    fields["funding_rate_current"] = market_snapshot.get("funding_rate_current")
    fields["funding_rate_8h_history"] = json.dumps(market_snapshot.get("funding_rate_8h_history", []))
    fields["open_interest_usd"] = market_snapshot.get("open_interest_usd")
    fields["open_interest_24h_change_pct"] = market_snapshot.get("open_interest_24h_change_pct")
    fields["liquidation_volume_1h_usd"] = market_snapshot.get("liquidation_volume_1h_usd")
    fields["liquidation_direction_dominant"] = market_snapshot.get("liquidation_direction_dominant")
    fields["regime"] = regime

    extra_context = {
        k: v for k, v in market_snapshot.items()
        if k not in _OHLCV_KEYS and k not in _DEDICATED_KEYS
    }
    fields["market_context_json"] = json.dumps(extra_context, default=str)

    if reasoning:
        fields["hypothesis"] = reasoning.get("hypothesis", "")
        fields["key_conditions_met"] = json.dumps(reasoning.get("key_conditions_met", []))
        fields["key_conditions_missing"] = json.dumps(reasoning.get("key_conditions_missing", []))
        fields["confidence"] = reasoning.get("confidence")
        fields["expected_value"] = reasoning.get("expected_value", "")
        fields["agent_reasoning_json"] = json.dumps(
            {k: reasoning.get(k) for k in _REASONING_KEYS}, default=str
        )

    _update_trade(conn, trade_id, fields)


def write_outcome(conn, trade_id: str, outcome: dict) -> None:
    """Update a trade row with outcome/postmortem fields at close.

    Only known outcome columns are written; unrecognized keys in `outcome`
    are ignored so callers can pass partial dicts (e.g. just
    {"agent_postmortem": "..."}).
    """
    fields = {k: v for k, v in outcome.items() if k in _OUTCOME_KEYS}
    if not fields:
        logger.debug("write_outcome(%s): no recognized outcome fields, skipping", trade_id)
        return
    _update_trade(conn, trade_id, fields)


def _update_trade(conn, trade_id: str, fields: dict) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{col}=?" for col in fields)
    conn.execute(
        f"UPDATE trades SET {set_clause} WHERE id=?",
        [*fields.values(), trade_id],
    )
    conn.commit()
