import json

import msgpack


def write_entry(conn, trade_id: str, asset_snapshot: dict, *, regime: str = "",
                reasoning: dict | None = None) -> None:
    ohlcv_15m = msgpack.packb(asset_snapshot.get("ohlcv_15m", []), use_bin_type=True)
    ohlcv_1h = msgpack.packb(asset_snapshot.get("ohlcv_1h", []), use_bin_type=True)
    ohlcv_4h = msgpack.packb(asset_snapshot.get("ohlcv_4h", []), use_bin_type=True)

    funding_hist = asset_snapshot.get("funding_rate_8h_history", [])
    funding_blob = msgpack.packb(funding_hist, use_bin_type=True)

    oi_data = {
        "open_interest_usd": asset_snapshot.get("open_interest_usd", 0),
        "open_interest_24h_change_pct": asset_snapshot.get("open_interest_24h_change_pct", 0),
    }
    oi_json = json.dumps(oi_data)

    liq_data = {
        "liquidation_volume_1h_usd": asset_snapshot.get("liquidation_volume_1h_usd", 0),
        "liquidation_direction_dominant": asset_snapshot.get("liquidation_direction_dominant", ""),
    }
    liq_json = json.dumps(liq_data)

    ev_text = (reasoning or {}).get("expected_value", "")
    hypothesis = (reasoning or {}).get("hypothesis", "")
    kcm = json.dumps((reasoning or {}).get("key_conditions_met", []))
    kcmiss = json.dumps((reasoning or {}).get("key_conditions_missing", []))
    confidence = (reasoning or {}).get("confidence", None)

    funding_rate_current = asset_snapshot.get("funding_rate_current", 0)
    oi_change_pct = asset_snapshot.get("open_interest_24h_change_pct", 0)

    conn.execute(
        """UPDATE trades SET ohlcv_15m_40_blob=?, ohlcv_1h_20_blob=?,
           ohlcv_4h_10_blob=?, funding_history_blob=?,
           oi_data_json=?, liquidation_data_json=?,
           regime=?, expected_value_text=?,
           funding_rate_current=?, open_interest_24h_change_pct=?,
           hypothesis=?, key_conditions_met=?, key_conditions_missing=?,
           confidence=? WHERE id=?""",
        (
            ohlcv_15m,
            ohlcv_1h,
            ohlcv_4h,
            funding_blob,
            oi_json,
            liq_json,
            regime,
            ev_text,
            funding_rate_current,
            oi_change_pct,
            hypothesis,
            kcm,
            kcmiss,
            confidence,
            trade_id,
        ),
    )
    conn.commit()


def pack_ohlcv(candles: list) -> bytes:
    return msgpack.packb(candles, use_bin_type=True)


def unpack_ohlcv(blob: bytes | None) -> list:
    if not blob:
        return []
    try:
        return msgpack.unpackb(blob, raw=False)
    except Exception:
        return []


def write_outcome(conn, trade_id: str, outcome_dict: dict) -> None:
    conn.execute(
        """UPDATE trades SET exit_price=?, exit_timestamp=?,
           exit_reason=?, duration_minutes=?, pnl_pct=?,
           pnl_usd=?, result=?, status=?, postmortem=?,
           agent_postmortem=? WHERE id=?""",
        (
            outcome_dict.get("exit_price"),
            outcome_dict.get("exit_timestamp"),
            outcome_dict.get("exit_reason"),
            outcome_dict.get("duration_minutes"),
            outcome_dict.get("pnl_pct"),
            outcome_dict.get("pnl_usd"),
            outcome_dict.get("result"),
            outcome_dict.get("status", "closed"),
            outcome_dict.get("postmortem"),
            outcome_dict.get("agent_postmortem"),
            trade_id,
        ),
    )
    conn.commit()
