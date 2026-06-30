from datetime import datetime, timezone

from store.db import (
    insert_trade,
    insert_position,
    get_positions,
    get_latest_account,
)
from execution.bridge import TradingBridge


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trade_id(agent_id: str, asset: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = asset.replace("-PERP", "")
    return f"{agent_id}_{ts}_{short}"


class PaperBridge(TradingBridge):
    def __init__(self, agent_id: str, conn, market_state: dict,
                 config: dict | None = None):
        self.agent_id = agent_id
        self.conn = conn
        self.market_state = market_state
        self.config = config

    def enter(self, order: dict) -> dict:
        asset = order["asset"]
        market = self.market_state.get(asset, {})
        fill_price = market.get("mid_price", order["entry_price"])

        account = self.get_account()
        balance = account["balance"]
        notional = balance * order["position_size_pct"]

        trade_id = _trade_id(self.agent_id, asset)
        pos_id = f"pos_{trade_id}"
        now = _now()

        trade = {
            "id": trade_id,
            "agent_id": self.agent_id,
            "thesis_version": 1,
            "account_balance_at_entry": balance,
            "mode": "paper",
            "asset": asset,
            "direction": order["direction"],
            "entry_price": fill_price,
            "stop_loss_price": order["stop_loss_price"],
            "take_profit_price": order["take_profit_price"],
            "leverage": order["leverage"],
            "position_size_pct": order["position_size_pct"],
            "notional_usd": notional,
            "entry_timestamp": now,
            "status": "open",
        }
        insert_trade(self.conn, trade)

        position = {
            "id": pos_id,
            "agent_id": self.agent_id,
            "asset": asset,
            "direction": order["direction"],
            "entry_price": fill_price,
            "stop_loss_price": order["stop_loss_price"],
            "take_profit_price": order["take_profit_price"],
            "leverage": order["leverage"],
            "position_size_pct": order["position_size_pct"],
            "notional_usd": notional,
            "opened_at": now,
            "mode": "paper",
            "trade_id": trade_id,
        }
        insert_position(self.conn, position)

        return {
            "trade_id": trade_id,
            "fill_price": fill_price,
            "notional_usd": notional,
            "timestamp": now,
        }

    def get_positions(self) -> list[dict]:
        return get_positions(self.conn, self.agent_id)

    def close(self, position_id: str, reason: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        if not row:
            return {}
        pos = dict(row)

        asset = pos["asset"]
        market = self.market_state.get(asset, {})
        exit_price = market.get("mid_price", pos["entry_price"])

        entry = pos["entry_price"]
        if pos["direction"] == "long":
            pnl_pct = (exit_price - entry) / entry * pos["leverage"]
        else:
            pnl_pct = (entry - exit_price) / entry * pos["leverage"]
        pnl_usd = pos["notional_usd"] * pnl_pct

        now = _now()
        account = self.get_account()
        new_balance = account["balance"] + pnl_usd
        peak = max(account["peak"], new_balance)

        # Single transaction: all three writes commit together or roll back together
        with self.conn:
            self.conn.execute(
                """UPDATE trades SET status='closed', exit_price=?, exit_timestamp=?,
                   exit_reason=?, pnl_pct=?, pnl_usd=?, result=? WHERE id=?""",
                (exit_price, now, reason, pnl_pct, pnl_usd,
                 "win" if pnl_pct > 0 else "loss", pos["trade_id"]),
            )
            self.conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
            self.conn.execute(
                "INSERT INTO accounts (agent_id, mode, balance, peak_balance, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (self.agent_id, "paper", new_balance, peak, now),
            )

        return {"exit_price": exit_price, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

    def get_account(self) -> dict:
        latest = get_latest_account(self.conn, self.agent_id, "paper")
        if latest:
            return {"balance": latest["balance"], "peak": latest["peak_balance"]}
        starting = (
            self.config["desk"]["starting_balance"]
            if self.config
            else 50000.0
        )
        return {"balance": starting, "peak": starting}
