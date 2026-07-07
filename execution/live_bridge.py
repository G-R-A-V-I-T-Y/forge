"""execution/live_bridge.py — Live trading via Hyperliquid Exchange API.

Each agent has its own Ethereum wallet (encrypted keystore on disk). Orders
are signed with the agent's private key and submitted to Hyperliquid's
Exchange API.

Architecture:
  - Uses the hyperliquid CCXT SDK only for EIP-712 signing and HTTP transport.
  - Constructs exchange actions manually (avoids broken CCXT market-loading).
  - Reads balance/positions via the public Info API (no signing needed).
  - Implements TradingBridge ABC so agents never know which bridge is active.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import eth_account
from eth_account import Account
from hyperliquid.ccxt.async_support.hyperliquid import hyperliquid as HyperliquidAsync

from execution.bridge import TradingBridge
from store.db import get_latest_account, get_positions, insert_position, insert_trade
from store.positions import execute_close

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"
EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"
_FAILURE_THRESHOLD = 5
_CIRCUIT_COOLDOWN = 60.0


def _load_signing_account(agent_id: str, password: str | None = None) -> eth_account.Account:
    keystore_dir = Path("data/keystores")
    keystore_path = keystore_dir / f"{agent_id}.json"
    if not keystore_path.exists():
        raise FileNotFoundError(
            f"Keystore not found for {agent_id}: {keystore_path}. "
            "Run scripts/onboard_trader.py first."
        )
    keystore = json.loads(keystore_path.read_text(encoding="utf-8"))
    if password is None:
        password = os.environ.get("FORGE_KEYSTORE_PASSWORD", "")
        if not password:
            raise RuntimeError(
                "FORGE_KEYSTORE_PASSWORD not set and no password provided"
            )
    private_key = Account.decrypt(keystore, password).hex()
    return Account.from_key(private_key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trade_id(agent_id: str, asset: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = asset.replace("-PERP", "")
    return f"{agent_id}_{ts}_{short}"


class LiveBridge(TradingBridge):
    """TradingBridge that submits real orders to Hyperliquid."""

    def __init__(
        self,
        agent_id: str,
        conn,
        config: dict,
        keystore_password: str | None = None,
    ):
        self.agent_id = agent_id
        self.conn = conn
        self.config = config or {}
        live_cfg = self.config.get("live", {})

        self._account = _load_signing_account(agent_id, keystore_password)
        self.address = self._account.address

        self._hl = HyperliquidAsync({
            "apiKey": self.address,
            "privateKey": self._account.key.hex(),
        })
        self._asset_index: dict[str, int] = {}
        self._universe_loaded: float = 0.0

        self._default_leverage = live_cfg.get("default_leverage", 5)
        self._is_cross = live_cfg.get("default_leverage_mode", "cross") == "cross"
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

    # ------------------------------------------------------------------
    # Asset index cache — Hyperliquid identifies assets by numeric index
    # in the universe, not by string name.
    # ------------------------------------------------------------------
    async def _refresh_universe(self) -> None:
        now = time.monotonic()
        if now - self._universe_loaded < 300:
            return
        info = await self._hl.public_post_info({"type": "metaAndAssetCtxs"})
        universe = info[0]["universe"]
        self._asset_index = {
            entry["name"]: i for i, entry in enumerate(universe)
        }
        self._universe_loaded = now

    async def _asset_id(self, asset: str) -> int:
        coin = asset.replace("-PERP", "")
        await self._refresh_universe()
        aid = self._asset_index.get(coin)
        if aid is None:
            raise KeyError(f"Asset {coin!r} not found in Hyperliquid universe")
        return aid

    # ------------------------------------------------------------------
    # Info API (read-only, no signing needed) via raw HTTP POST
    # ------------------------------------------------------------------
    async def _info_post(self, body: dict) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(INFO_URL, json=body)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Exchange API (signed) via CCXT SDK
    # ------------------------------------------------------------------
    async def _exchange_post(self, action: dict) -> dict:
        now = time.monotonic()
        if now < self._circuit_open_until:
            raise RuntimeError(
                f"LiveBridge circuit breaker open for {self.agent_id} "
                f"({self._circuit_open_until - now:.0f}s remaining)"
            )
        nonce = int(time.time() * 1000)
        signature = self._hl.sign_l1_action(action, nonce)
        body = {"action": action, "nonce": nonce, "signature": signature}
        try:
            result = await self._hl.private_post_exchange(body)
            self._consecutive_failures = 0
            return result
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _FAILURE_THRESHOLD:
                self._circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN
                logger.error(
                    "[%s] LiveBridge circuit breaker opened after %d failures",
                    self.agent_id,
                    self._consecutive_failures,
                )
            raise

    # ------------------------------------------------------------------
    # TradingBridge interface
    # ------------------------------------------------------------------
    async def enter(self, order: dict) -> dict:
        asset_raw = order["asset"]
        asset = asset_raw.replace("-PERP", "")
        direction = order["direction"]
        is_buy = direction == "long"
        position_size_pct = order.get("position_size_pct", 0.1)
        leverage = order.get("leverage", self._default_leverage)

        user_state = await self._info_post({
            "type": "clearinghouseState",
            "user": self.address,
        })
        balance = float(user_state.get("withdrawable", "0"))
        if balance <= 0:
            raise RuntimeError(
                f"Account {self.address} has no withdrawable balance. Fund it first."
            )

        all_mids = await self._info_post({"type": "allMids"})
        mid_price = float(all_mids.get(asset, "0"))
        if mid_price <= 0:
            raise RuntimeError(f"Cannot determine price for {asset}")

        notional = balance * position_size_pct
        size = round(notional / mid_price, 4)
        if size <= 0:
            raise RuntimeError(f"Calculated size too small for {asset}")

        aid = await self._asset_id(asset)
        action: dict = {
            "type": "order",
            "orders": [
                {
                    "a": aid,
                    "b": is_buy,
                    "p": str(mid_price),
                    "s": str(size),
                    "r": False,
                    "t": {"limit": {"tif": "Ioc"}},
                }
            ],
        }
        order_result = await self._exchange_post(action)

        response = order_result.get("response", {})
        statuses = response.get("data", {}).get("statuses", []) if response.get("data") else []
        if response.get("type") == "error":
            err_msg = response.get("data", "") or str(order_result)
            logger.error("[%s] Order rejected: %s", self.agent_id, err_msg)
            raise RuntimeError(f"Hyperliquid order rejected: {err_msg}")

        fills = [s["filled"] for s in statuses if "filled" in s]
        if not fills:
            logger.warning(
                "[%s] Order %s had no fills: %s", self.agent_id, asset, order_result
            )
            return {"trade_id": None, "fill_price": 0, "notional_usd": 0, "timestamp": None}

        total_sz = sum(float(f["sz"]) for f in fills)
        avg_price = sum(float(f["avgPx"]) * float(f["sz"]) for f in fills) / total_sz
        filled_notional = avg_price * total_sz
        now = _now()

        trade_id = _trade_id(self.agent_id, asset_raw)
        trade = {
            "id": trade_id,
            "agent_id": self.agent_id,
            "thesis_version": order.get("thesis_version", 1),
            "account_balance_at_entry": balance,
            "mode": "live",
            "asset": asset_raw,
            "direction": direction,
            "entry_price": avg_price,
            "stop_loss_price": order.get("stop_loss_price"),
            "take_profit_price": order.get("take_profit_price"),
            "leverage": leverage,
            "position_size_pct": position_size_pct,
            "notional_usd": filled_notional,
            "entry_timestamp": now,
            "status": "open",
        }
        insert_trade(self.conn, trade)

        position = {
            "id": f"pos_{trade_id}",
            "agent_id": self.agent_id,
            "asset": asset_raw,
            "direction": direction,
            "entry_price": avg_price,
            "stop_loss_price": order.get("stop_loss_price"),
            "take_profit_price": order.get("take_profit_price"),
            "leverage": leverage,
            "position_size_pct": position_size_pct,
            "notional_usd": filled_notional,
            "opened_at": now,
            "mode": "live",
            "trade_id": trade_id,
        }
        insert_position(self.conn, position)

        logger.info(
            "[%s] Live entry %s %s @ %.2f (%.2f USD)",
            self.agent_id, direction.upper(), asset, avg_price, filled_notional,
        )
        return {
            "trade_id": trade_id,
            "fill_price": avg_price,
            "notional_usd": filled_notional,
            "timestamp": now,
        }

    def get_positions(self) -> list[dict]:
        try:
            state = self._info_post({
                "type": "clearinghouseState",
                "user": self.address,
            })
            # clearinghouseState doesn't include asset positions directly,
            # so fetch userState instead.
        except Exception:
            pass
        # Synchronous fallback to local DB is handled via try/except below
        try:
            import httpx

            resp = httpx.post(INFO_URL, json={
                "type": "userState",
                "user": self.address,
            }, timeout=15)
            resp.raise_for_status()
            state = resp.json()
            asset_positions = state.get("assetPositions", [])
            if not asset_positions:
                return []
            live = []
            for ap in asset_positions:
                p = ap["position"]
                szi = float(p["szi"])
                if szi == 0:
                    continue
                live.append({
                    "asset": p["coin"] + "-PERP",
                    "direction": "long" if szi > 0 else "short",
                    "size": abs(szi),
                    "entry_price": float(p["entryPx"]),
                    "unrealized_pnl": float(p["unrealizedPnl"]),
                    "leverage": int(float(p.get("leverage", "1"))),
                    "liquidation_price": (
                        float(p["liquidationPx"]) if p.get("liquidationPx") else None
                    ),
                })
            return live
        except Exception:
            logger.warning(
                "[%s] Failed to fetch live positions, falling back to local",
                self.agent_id,
                exc_info=True,
            )
            return get_positions(self.conn, self.agent_id)

    async def close(self, position_id: str, reason: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        if not row:
            return {}
        pos = dict(row)

        asset_raw = pos["asset"]
        asset = asset_raw.replace("-PERP", "")
        direction = pos["direction"]
        is_buy = direction != "long"
        size = round(pos["notional_usd"] / pos["entry_price"], 4)

        all_mids = await self._info_post({"type": "allMids"})
        exit_price = float(all_mids.get(asset, "0"))
        if exit_price <= 0:
            raise RuntimeError(f"Cannot determine price for {asset}")

        aid = await self._asset_id(asset)
        action: dict = {
            "type": "order",
            "orders": [
                {
                    "a": aid,
                    "b": is_buy,
                    "p": str(exit_price),
                    "s": str(size),
                    "r": True,
                    "t": {"limit": {"tif": "Ioc"}},
                }
            ],
        }
        await self._exchange_post(action)

        taker_fee = self.config.get("desk", {}).get("taker_fee", 0.00035)
        result = execute_close(
            conn=self.conn,
            position_id=position_id,
            exit_price=exit_price,
            reason=reason,
            config={"taker_fee": taker_fee},
            position_dict=pos,
            funding_history=[],
        )

        logger.info(
            "[%s] Live close %s %s @ %.2f (%s)",
            self.agent_id, direction.upper(), asset, exit_price, reason,
        )
        return result

    async def get_account(self) -> dict:
        try:
            state = await self._info_post({
                "type": "clearinghouseState",
                "user": self.address,
            })
            account_value = float(state.get("withdrawable", "0"))
            return {"balance": account_value, "peak": account_value}
        except Exception:
            logger.warning(
                "[%s] Failed to fetch live account state, falling back to local",
                self.agent_id,
                exc_info=True,
            )
            latest = get_latest_account(self.conn, self.agent_id, "live")
            if latest:
                return {"balance": latest["balance"], "peak": latest["peak_balance"]}
            return {"balance": 0, "peak": 0}

    async def close_client(self) -> None:
        await self._hl.close()
