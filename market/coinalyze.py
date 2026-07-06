"""Coinalyze REST API client for liquidation data.

Coinalyze aggregates liquidation data across multiple exchanges.
Each request to the liquidation-history endpoint counts as N API calls
where N = number of symbols requested (max 20 per request).
Rate limit: 40 calls/minute per API key.

Official API docs: https://api.coinalyze.net/v1/doc/

Symbol mapping from project format (Hyperliquid `-PERP` suffix) to
Coinalyze format:

    Project:    BTC-PERP  →  Coinalyze:  BTCUSDT_PERP.A
    Project:    ETH-PERP  →  Coinalyze:  ETHUSDT_PERP.A
    ...

Coinalyze's symbol format is ``{BASE}{QUOTE}_PERP.{EXCHANGE_CODE}`` where
``.A`` = Binance (the largest perp venue).  Coinalyze does not provide
exchange-specific liquidation breakdowns, so the data represents the
aggregated liquidation volume across all supported venues for the given
symbol.

Auth: API key via ``COINALYZE_API_KEY`` environment variable, or a
``coinalyze.api_key`` entry in ``config.yaml``::

    coinalyze:
      api_key: "..."
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coinalyze.net/v1/"
LIQUIDATION_ENDPOINT = "liquidation-history"

# 15-minute lookback window — matches steel_crane thesis requirement for
# "liquidation volume magnitude in the last 15 minutes".
LIQUIDATION_LOOKBACK_MINUTES = 15

# Coinalyze free tier: 40 calls/minute, max 20 symbols per request.
# With 20 assets × 1 call each = 20 calls per heartbeat cycle, well within
# the 40-call/minute budget.
MAX_SYMBOLS_PER_REQUEST = 20


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

def project_to_coinalyze_symbol(project_asset: str) -> str | None:
    """Map a project asset name (e.g. ``BTC-PERP``) to a Coinalyze futures
    symbol (e.g. ``BTCUSDT_PERP.A``).

    Coinalyze uses the format ``{BASE}{QUOTE}_PERP.{EXCHANGE_CODE}`` where
    ``.A`` is Binance (the dominant perp venue).  The quote asset is
    consistently ``USDT`` for the assets in our tracked universe.

    Returns ``None`` if the asset is not covered by Coinalyze.
    """
    # Strip the -PERP suffix (case-insensitive) to get the base asset name
    base = project_asset.upper().replace("-PERP", "")
    coinalyze_symbol = f"{base}USDT_PERP.A"
    return coinalyze_symbol


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CoinalyzeClient:
    """Async client for Coinalyze liquidation-history endpoint.

    Implements a simple circuit breaker (5 consecutive failures → open for
    60s) mirroring the convention used in HyperliquidClient so heartbeat
    degrades gracefully when Coinalyze is unreachable.
    """

    _MAX_RETRIES = 3
    _FAILURE_THRESHOLD = 5
    _CIRCUIT_COOLDOWN = 60.0

    def __init__(self, api_key: str | None = None):
        if api_key is None:
            api_key = os.environ.get("COINALYZE_API_KEY")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0
        self.available = True
        self._circuit_lock = asyncio.Lock()

    async def __aenter__(self):
        headers = {}
        if self._api_key:
            headers["api_key"] = self._api_key
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=headers,
            timeout=15.0,
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, endpoint: str, params: dict) -> dict | None:
        """GET request with rate-limit backoff and circuit breaker."""
        if not self._api_key:
            logger.warning("CoinalyzeClient: no API key configured, skipping")
            return None

        if not self.available:
            if time.monotonic() < self._circuit_open_until:
                logger.debug(
                    "CoinalyzeClient: circuit breaker open — skipping %s",
                    endpoint,
                )
                return None
            async with self._circuit_lock:
                if not self.available:
                    self.available = True
                    self._consecutive_failures = 0
                    logger.info("CoinalyzeClient: circuit breaker reset, retrying")

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = await self._client.get(endpoint, params=params)
                if resp.status_code == 429:
                    retry_after = float(
                        resp.headers.get("Retry-After", 1)
                    )
                    logger.warning(
                        "CoinalyzeClient rate limited, waiting %.1fs",
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                self._consecutive_failures = 0
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                logger.debug(
                    "CoinalyzeClient: attempt %d/%d failed for %s",
                    attempt + 1, self._MAX_RETRIES, endpoint,
                )
                if attempt < self._MAX_RETRIES - 1:
                    await asyncio.sleep(2.0 ** attempt)
            except Exception as exc:
                last_exc = exc
                if attempt < self._MAX_RETRIES - 1:
                    await asyncio.sleep(2.0 ** attempt)

        self._record_failure()
        return None

    def _record_failure(self):
        if self.available:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._FAILURE_THRESHOLD:
                self.available = False
                self._circuit_open_until = (
                    time.monotonic() + self._CIRCUIT_COOLDOWN
                )
                logger.error(
                    "CoinalyzeClient: %d consecutive failures — circuit breaker opened",
                    self._consecutive_failures,
                )

    async def fetch_liquidations(
        self,
        coinalyze_symbols: list[str],
        lookback_minutes: int = LIQUIDATION_LOOKBACK_MINUTES,
    ) -> dict[str, list[dict]] | None:
        """Fetch liquidation history for a batch of Coinalyze symbols.

        Returns a dict mapping each symbol to its history list
        ``[{t, l, s}, ...]`` where ``t``=timestamp (UNIX seconds),
        ``l``=long liquidation volume (in base asset),
        ``s``=short liquidation volume (in base asset).

        If ``convert_to_usd=True`` the volumes are denominated in USD
        instead.  Returns ``None`` on complete failure.
        """
        now = int(time.time())
        from_ts = now - lookback_minutes * 60

        # Split into batches of MAX_SYMBOLS_PER_REQUEST
        batches = [
            coinalyze_symbols[i:i + MAX_SYMBOLS_PER_REQUEST]
            for i in range(0, len(coinalyze_symbols), MAX_SYMBOLS_PER_REQUEST)
        ]

        all_results: dict[str, list[dict]] = {}

        for batch in batches:
            params = {
                "symbols": ",".join(batch),
                "interval": "1min",
                "from": from_ts,
                "to": now,
                "convert_to_usd": "true",
            }
            data = await self._get(LIQUIDATION_ENDPOINT, params)
            if data is None:
                continue
            for entry in data:
                symbol = entry.get("symbol", "")
                history = entry.get("history", [])
                all_results[symbol] = history

        return all_results if all_results else None

    async def fetch_liquidations_for_assets(
        self,
        project_assets: list[str],
        lookback_minutes: int = LIQUIDATION_LOOKBACK_MINUTES,
    ) -> dict[str, dict[str, float | None]]:
        """Fetch liquidations for project assets and compute per-asset
        liquidation features.

        Returns a dict mapping each project asset to a feature dict:
        ``{"liq_total_usd": float|None, "liq_long_usd": float|None,
        "liq_short_usd": float|None}`` — or ``None`` for all if the
        fetch failed entirely.

        Assets whose Coinalyze symbol is not covered are silently
        skipped (their features are ``None``).
        """
        # Build symbol mapping
        symbol_map: dict[str, str] = {}  # project_asset → coinalyze_symbol
        for asset in project_assets:
            coinalyze_sym = project_to_coinalyze_symbol(asset)
            if coinalyze_sym:
                symbol_map[asset] = coinalyze_sym

        if not symbol_map:
            return {a: None for a in project_assets}

        coinalyze_symbols = list(symbol_map.values())
        data = await self.fetch_liquidations(coinalyze_symbols, lookback_minutes)

        if data is None:
            return {a: None for a in project_assets}

        # Build result: map each project asset to its liquidation features
        result: dict[str, dict[str, float | None]] = {}
        for asset, coinalyze_sym in symbol_map.items():
            history = data.get(coinalyze_sym)
            if not history:
                result[asset] = None
                continue

            total = 0.0
            longs = 0.0
            shorts = 0.0
            for point in history:
                longs += point.get("l", 0) or 0
                shorts += point.get("s", 0) or 0
            total = longs + shorts

            result[asset] = {
                "liq_total_usd": total,
                "liq_long_usd": longs,
                "liq_short_usd": shorts,
            }

        return result
