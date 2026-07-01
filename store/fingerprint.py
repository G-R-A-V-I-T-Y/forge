import json

import msgpack


def write_entry(conn, trade_id: str, market_state: dict, agent_reasoning: dict) -> None:
    asset = agent_reasoning.get("asset", "")
    sd = market_state.get(asset, {})

    ohlcv_15m = msgpack.packb(sd.get("ohlcv_15m", []), use_bin_type=True)
    ohlcv_1h = msgpack.packb(sd.get("ohlcv_1h", []), use_bin_type=True)
    ohlcv_4h = msgpack.packb(sd.get("ohlcv_4h", []), use_bin_type=True)

    funding_hist = sd.get("funding_rate_8h_history", [])
    funding_blob = msgpack.packb(funding_hist, use_bin_type=True)

    oi_data = {
        "open_interest_usd": sd.get("open_interest_usd", 0),
        "open_interest_24h_change_pct": sd.get("open_interest_24h_change_pct", 0),
    }
    oi_json = json.dumps(oi_data)

    liq_data = {
        "liquidation_volume_1h_usd": sd.get("liquidation_volume_1h_usd", 0),
        "liquidation_direction_dominant": sd.get("liquidation_direction_dominant", ""),
    }
    liq_json = json.dumps(liq_data)

    regime = market_state.get("_regime", "")
    ev_text = agent_reasoning.get("expected_value", "")

    conn.execute(
        """UPDATE trades SET ohlcv_15m_40_blob=?, ohlcv_1h_20_blob=?,
           ohlcv_4h_10_blob=?, funding_history_blob=?,
           oi_data_json=?, liquidation_data_json=?,
           regime=?, expected_value_text=? WHERE id=?""",
        (
            ohlcv_15m,
            ohlcv_1h,
            ohlcv_4h,
            funding_blob,
            oi_json,
            liq_json,
            regime,
            ev_text,
            trade_id,
        ),
    )
    conn.commit()


def write_outcome(conn, trade_id: str, outcome_dict: dict) -> None:
    conn.execute(
        """UPDATE trades SET exit_price=?, exit_timestamp=?,
           exit_reason=?, duration_minutes=?, pnl_pct=?,
           pnl_usd=?, result=?, status=?, postmortem=? WHERE id=?""",
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
            trade_id,
        ),
    )
    conn.commit()
