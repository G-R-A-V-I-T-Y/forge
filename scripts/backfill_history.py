#!/usr/bin/env python
"""scripts/backfill_history.py -- one-time historical backfill into the ledger.

Backfills 1h candles + funding (12 months) and 5m candles (90 days) for the
full universe directly into ledger/{kind}/{YYYY-MM}.{jsonl,parquet} via
store.ledger.append_ledger_record, dated by each candle's own historical
timestamp (not "now"), so backfilled rows compact and decay through the
exact same monthly pipeline as organically-captured data.

OI and liquidations are NOT backfilled -- Hyperliquid has no OI history
endpoint and Coinalyze's free tier doesn't backfill either; both remain
live-accumulated only, per docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import yaml

from market.hyperliquid import HyperliquidClient
from store.ledger import append_ledger_record

DEFAULT_CANDLE_MONTHS = 12
DEFAULT_5M_DAYS = 90


async def _backfill_asset_1h_and_funding(client: HyperliquidClient, asset: str, months: int) -> dict:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - months * 30 * 24 * 3600 * 1000

    candles = await client.get_ohlcv(asset, "1h", lookback_candles=0, start_ms=start_ms, end_ms=end_ms)
    for c in candles:
        ts, o, h, l, close, v = c
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "candles_1h", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
                           "o": o, "h": h, "l": l, "c": close, "v": v},
            when,
        )

    funding = await client.get_funding_history(asset, start_ms)
    for f in funding:
        rate = f.get("fundingRate")
        ts = f.get("time")
        if rate is None or ts is None:
            continue
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "funding", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset, "rate": rate},
            when,
        )

    return {"asset": asset, "candles_1h": len(candles), "funding": len(funding)}


async def _backfill_asset_5m(client: HyperliquidClient, asset: str, days: int) -> dict:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000

    candles = await client.get_ohlcv(asset, "5m", lookback_candles=0, start_ms=start_ms, end_ms=end_ms)
    for c in candles:
        ts, o, h, l, close, v = c
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "candles_5m", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
                           "o": o, "h": h, "l": l, "c": close, "v": v},
            when,
        )
    return {"asset": asset, "candles_5m": len(candles)}


async def backfill(
    universe: list[str], provider: HyperliquidClient,
    months: int = DEFAULT_CANDLE_MONTHS, days_5m: int = DEFAULT_5M_DAYS,
) -> dict:
    """Backfill 1h candles + funding (`months` back) and 5m candles
    (`days_5m` back) for every asset in `universe`. Returns a per-asset
    summary dict. Best-effort per asset -- one asset's failure is logged
    and does not stop the rest (matches append_ledger_record's own
    never-block contract for the writes themselves; the network fetch here
    is the one part of this script that CAN legitimately fail per-asset)."""
    results = {}
    for asset in universe:
        try:
            hourly = await _backfill_asset_1h_and_funding(provider, asset, months)
            five_min = await _backfill_asset_5m(provider, asset, days_5m)
            results[asset] = {**hourly, **five_min}
            print(f"{asset}: {results[asset]}")
        except Exception as exc:
            results[asset] = {"error": str(exc)}
            print(f"{asset}: FAILED - {exc}")
    return results


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical candles+funding into the ledger")
    parser.add_argument("--months", type=int, default=DEFAULT_CANDLE_MONTHS)
    parser.add_argument("--days-5m", type=int, default=DEFAULT_5M_DAYS)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    universe = config["universe"]

    # HyperliquidClient() takes no arguments and is an async context manager
    # (market/hyperliquid.py:46-66; market/provider.py:16 constructs it the
    # same bare way) -- not a config-taking constructor with a close() method.
    async with HyperliquidClient() as client:
        await backfill(universe, client, args.months, args.days_5m)


if __name__ == "__main__":
    asyncio.run(_main())
