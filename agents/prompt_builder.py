from store.db import get_trades, get_positions, get_latest_account


def build_decision_prompt(agent_id: str, thesis_text: str,
                          market_state: dict, conn,
                          starting_balance: float = 50000.0) -> str:
    """
    Assembles the full decision prompt from:
    1. Thesis text
    2. Performance summary (from accounts table — balance, peak, drawdown)
    3. Last 10 closed trades (from trades table)
    4. Current open positions (from positions table)
    5. Market state summary (top 5 assets by abs(funding_rate_current))
    6. Decision instruction with JSON format examples
    """
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

    # Format last 10 closed trades
    trade_lines = []
    for t in closed_trades:
        pnl = t.get("pnl_pct", 0) or 0
        trade_lines.append(
            f"  {t['asset']} {t['direction']} | PnL: {pnl:+.2%} | "
            f"exit: {t.get('exit_reason', '?')}"
        )
    trades_section = "\n".join(trade_lines) if trade_lines else "  No closed trades yet."

    # Format open positions
    pos_lines = []
    for p in open_positions:
        pos_lines.append(
            f"  {p['asset']} {p['direction']} @ {p['entry_price']:.4f} | "
            f"SL: {p['stop_loss_price']:.4f} | TP: {p['take_profit_price']:.4f}"
        )
    positions_section = "\n".join(pos_lines) if pos_lines else "  No open positions."

    # Format market state summary (top 5 assets by absolute funding rate)
    sorted_assets = sorted(
        market_state.items(),
        key=lambda kv: abs(kv[1].get("funding_rate_current", 0)),
        reverse=True,
    )[:5]
    market_lines = []
    for asset, data in sorted_assets:
        market_lines.append(
            f"  {asset:12s} price={data['mid_price']:.4f} "
            f"funding={data['funding_rate_current']:+.4%} "
            f"OI_24h_chg={data['open_interest_24h_change_pct']:+.1f}%"
        )
    market_section = "\n".join(market_lines)

    return f"""=== YOUR THESIS ===
{thesis_text}

=== PERFORMANCE SUMMARY ===
Account: ${balance:,.2f} | Peak: ${peak:,.2f} | Current DD: {dd_pct:.1%}
Closed trades: {len(closed_trades)} | Win rate: {win_rate:.0%}

=== LAST 10 CLOSED TRADES ===
{trades_section}

=== YOUR OPEN POSITIONS ===
{positions_section}

=== MARKET STATE (top 5 by funding magnitude) ===
{market_section}

=== DECISION ===
Based on your thesis, your performance record, and current market conditions, make a decision.
You may:
  - Enter a new trade: {{"action": "enter", "asset": "...", "direction": "long|short", "entry_price": 0.0, "stop_loss_price": 0.0, "take_profit_price": 0.0, "leverage": 1, "position_size_pct": 0.10, "hypothesis": "...", "key_conditions_met": [], "key_conditions_missing": [], "confidence": 0.0, "expected_value": "..."}}
  - Wait: {{"action": "wait", "reason": "..."}}
  - Close a position: {{"action": "close", "position_id": "...", "reason": "..."}}

Output JSON only."""
