from store.db import get_trades, get_positions, get_latest_account


async def build_decision_prompt(agent_id: str, thesis_text: str,
                                market_state: dict, conn, provider,
                                starting_balance: float = 50000.0) -> str:
    account = get_latest_account(conn, agent_id, "paper") or {
        "balance": starting_balance,
        "peak_balance": starting_balance,
    }
    balance = account["balance"]
    peak = account["peak_balance"]
    dd_pct = (peak - balance) / peak if peak > 0 else 0.0

    all_trades = get_trades(conn, agent_id, limit=10)
    closed_trades = [t for t in all_trades if t.get("status") == "closed"]
    wins = [t for t in closed_trades if t.get("result") == "win"]
    win_rate = len(wins) / len(closed_trades) if closed_trades else 0.0

    open_positions = get_positions(conn, agent_id)

    trade_lines = []
    for t in closed_trades:
        pnl = t.get("pnl_pct", 0) or 0
        trade_lines.append(
            f"  {t['asset']} {t['direction']} | PnL: {pnl:+.2%} | "
            f"exit: {t.get('exit_reason', '?')}"
        )
    trades_section = "\n".join(trade_lines) if trade_lines else "  No closed trades yet."

    pos_lines = []
    for p in open_positions:
        pos_lines.append(
            f"  {p['asset']} {p['direction']} @ {p['entry_price']:.4f} | "
            f"SL: {p['stop_loss_price']:.4f} | TP: {p['take_profit_price']:.4f}"
        )
    positions_section = "\n".join(pos_lines) if pos_lines else "  No open positions."

    assets_only = {k: v for k, v in market_state.items() if isinstance(v, dict)}
    sorted_assets = sorted(
        assets_only.items(),
        key=lambda kv: abs(kv[1].get("funding_rate_current", 0)),
        reverse=True,
    )
    market_lines = []
    for asset, data in sorted_assets:
        funding = data.get("funding_rate_current", 0)
        oi_chg = data.get("open_interest_24h_change_pct", 0)
        liq_vol = data.get("liquidation_volume_1h_usd", 0)
        liq_dir = data.get("liquidation_direction_dominant", "?")
        market_lines.append(
            f"  {asset:12s} price={data['mid_price']:.4f} "
            f"funding={funding:+.4%} OI_chg={oi_chg:+.1f}% "
            f"liq={liq_vol:,.0f}({liq_dir})"
        )
    market_section = "\n".join(market_lines)

    regime = market_state.get("_regime", "range_low_vol")

    return f"""=== YOUR THESIS ===
{thesis_text}

=== PERFORMANCE SUMMARY ===
Account: ${balance:,.2f} | Peak: ${peak:,.2f} | Current DD: {dd_pct:.1%}
Closed trades: {len(closed_trades)} | Win rate: {win_rate:.0%}

=== LAST 10 CLOSED TRADES ===
{trades_section}

=== YOUR OPEN POSITIONS ===
{positions_section}

=== MARKET REGIME ===
{regime}

=== MARKET STATE (all {len(sorted_assets)} assets) ===
{market_section}

=== DECISION ===
Based on your thesis, your performance record, and current market conditions, make a decision.
You may:
  - Enter a new trade: {{"action": "enter", "asset": "...", "direction": "long|short", "entry_price": 0.0, "stop_loss_price": 0.0, "take_profit_price": 0.0, "leverage": 1, "position_size_pct": 0.10, "hypothesis": "...", "key_conditions_met": [], "key_conditions_missing": [], "confidence": 0.0, "expected_value": "..."}}
  - Wait: {{"action": "wait", "reason": "..."}}
  - Close a position: {{"action": "close", "position_id": "...", "reason": "..."}}

Output JSON only."""
