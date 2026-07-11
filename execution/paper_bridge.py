import secrets
from datetime import datetime, timezone

from execution.bridge import TradingBridge
from execution.costs import compute_true_notional
from market.heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    heartbeat_max_age_seconds,
    read_heartbeat_or_none,
)
from store.db import get_latest_account, get_positions, insert_position, insert_trade
from store.positions import execute_close


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trade_id(agent_id: str, asset: str) -> str:
    """Generate a collision-proof trade ID.

    Uses a 4-byte hex suffix from secrets.token_bytes to avoid PK collisions
    when the same agent enters the same asset within the same second.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = asset.replace("-PERP", "")
    suffix = secrets.token_bytes(2).hex()
    return f"{agent_id}_{ts}_{short}_{suffix}"


class PaperBridge(TradingBridge):
    def __init__(self, agent_id: str, conn, provider, config: dict | None = None):
        self.agent_id = agent_id
        self.conn = conn
        self.provider = provider
        self.config = config

    async def _fill_price(self, asset: str, direction: str = "long") -> float:
        """Read the asset's current price from the shared heartbeat file
        (data/heartbeat.json by default) rather than calling the provider
        live — see docs/superpowers/specs/2026-07-01-heartbeat-wiring-design.md.
        A fill can't proceed without a price, so a missing/stale heartbeat or
        an asset absent from it is a hard failure: raises RuntimeError, which
        propagates up through enter()/close() to run_decision()'s outer
        except Exception, surfacing as {"action": "error", ...} rather than
        crashing the agent's tick.

        Applies spread and slippage to the raw heartbeat price for realistic
        paper fills.
        """
        desk_config = (self.config or {}).get("desk", {})
        heartbeat_path = desk_config.get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
        max_age = heartbeat_max_age_seconds(self.config or {})
        heartbeat = read_heartbeat_or_none(heartbeat_path, max_age)
        if heartbeat is None:
            raise RuntimeError(
                "heartbeat data unavailable or stale; cannot simulate fill"
            )

        asset_fields = (heartbeat.get("assets") or {}).get(asset)
        price = asset_fields.get("price") if asset_fields else None
        if price is None or price <= 0:
            raise RuntimeError(
                f"heartbeat data unavailable or stale; cannot simulate fill for {asset}"
            )

        # Apply spread: for long entries, add half-spread; for short entries, subtract half-spread
        spread = asset_fields.get("spread", 0)
        if spread and spread > 0:
            if direction == "short":
                price -= spread / 2
            else:
                price += spread / 2

        # Apply slippage estimate
        slippage_estimate = asset_fields.get("slippage_estimate", 0)
        if slippage_estimate and slippage_estimate > 0:
            if direction == "short":
                price -= slippage_estimate
            else:
                price += slippage_estimate

        return price

    async def enter(self, order: dict) -> dict:
        asset = order["asset"]
        fill_price = await self._fill_price(asset, order["direction"])

        account = await self.get_account()
        balance = account["balance"]
        margin = balance * order["position_size_pct"]
        leverage = order["leverage"]
        true_notional = compute_true_notional(balance, order["position_size_pct"], leverage)

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
            "leverage": leverage,
            "position_size_pct": order["position_size_pct"],
            "notional_usd": margin,
            "true_notional": true_notional,
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
            "leverage": leverage,
            "position_size_pct": order["position_size_pct"],
            "notional_usd": margin,
            "true_notional": true_notional,
            "opened_at": now,
            "max_hold_hours": order.get("max_hold_hours", 48.0),
            "mode": "paper",
            "trade_id": trade_id,
        }
        insert_position(self.conn, position)

        return {
            "trade_id": trade_id,
            "fill_price": fill_price,
            "notional_usd": margin,
            "true_notional": true_notional,
            "timestamp": now,
        }

    def get_positions(self) -> list[dict]:
        return get_positions(self.conn, self.agent_id)

    async def close(self, position_id: str, reason: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        if not row:
            return {}
        pos = dict(row)

        asset = pos["asset"]
        exit_price = await self._fill_price(asset, pos["direction"])

        desk_config = (self.config or {}).get("desk", {})
        heartbeat_path = desk_config.get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
        max_age = heartbeat_max_age_seconds(self.config or {})
        heartbeat = read_heartbeat_or_none(heartbeat_path, max_age)
        funding_history = []
        if heartbeat:
            asset_data = (heartbeat.get("assets") or {}).get(asset, {})
            funding_history = asset_data.get("funding_history", []) or []

        taker_fee = desk_config.get("taker_fee", 0.00035)

        return execute_close(
            conn=self.conn,
            position_id=position_id,
            exit_price=exit_price,
            reason=reason,
            config={"taker_fee": taker_fee},
            position_dict=pos,
            funding_history=funding_history,
        )

    async def get_account(self) -> dict:
        latest = get_latest_account(self.conn, self.agent_id, "paper")
        if latest:
            return {"balance": latest["balance"], "peak": latest["peak_balance"]}
        starting = self.config["desk"]["starting_balance"] if self.config else 50000.0
        return {"balance": starting, "peak": starting}
