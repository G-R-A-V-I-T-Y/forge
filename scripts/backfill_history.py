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

from market.hyperliquid import HyperliquidClient, _interval_to_ms
from store.ledger import append_ledger_record

DEFAULT_CANDLE_MONTHS = 12
DEFAULT_5M_DAYS = 90

# candleSnapshot's own single-response page size is small enough that a
# multi-month request silently returns only its first page rather than an
# error -- a real 12-month "1h" backfill measured 5003 rows back (~7 months),
# not ~8760, with no indication anything was truncated. When a
# [startTime, endTime] range exceeds the cap, candleSnapshot truncates from
# the START of the range and keeps the candles nearest endTime (confirmed
# empirically: an un-paginated 12-month request returned Dec-2025..Jul-2026,
# the most RECENT ~7 months, not the oldest) -- the opposite of
# fundingHistory, which has no endTime and always returns the earliest N
# candles after startTime. So candles must page BACKWARD (shrinking end_ms
# from the last-received page's oldest candle) while funding pages FORWARD
# (growing start_ms from the last-received page's newest entry) -- see
# _paginated_get_funding_history below.
#
# IMPORTANT, discovered after implementing backward pagination: for candles
# specifically (unlike funding), paginating further back does not actually
# yield more data. Every asset lands on the same ~5000-row ceiling (~208
# days for "1h", ~17 days for "5m") regardless of pagination direction --
# the second page's request consistently comes back empty. This means
# candleSnapshot's public endpoint appears to have a genuine historical
# depth ceiling around 5000 candles per interval, not merely a per-response
# page cap that pagination can walk past. The backward-pagination logic
# below is still correct (it fetches everything actually available and
# stops cleanly rather than assuming a fixed truncation direction that
# happened to coincidentally match observed output before), and it is what
# correctly fixed fundingHistory (500 -> 8640 rows, the real 12-month
# depth). But callers should not expect DEFAULT_CANDLE_MONTHS=12 or
# DEFAULT_5M_DAYS=90 to be fully achievable for candles via this endpoint --
# see docs/superpowers/reports/2026-07-07-seed-backtest-results.md for the
# actual per-stream data window a real backfill run produced. Capped
# generously to guard against an unexpected non-advancing response looping
# forever against a real network API.
MAX_PAGES_PER_ASSET = 500

# Paginating turns what used to be one request per asset/stream into several
# -- across the full universe that's enough added request volume to trip
# Hyperliquid's rate limiter well before HyperliquidClient's own 3-retry
# budget recovers (observed: 7/20 assets failed outright on the first
# paginated run). A small pause between page requests keeps steady-state
# request rate down without touching HyperliquidClient's shared retry/
# backoff logic, which the live trading path also depends on.
PAGE_REQUEST_DELAY_SECONDS = 0.3


async def _paginated_get_ohlcv(
    client: HyperliquidClient, asset: str, interval: str, start_ms: int, end_ms: int,
) -> list[list]:
    """Fetch every candle in [start_ms, end_ms), walking BACKWARD one page at
    a time from end_ms. candleSnapshot truncates from the start of an
    over-wide range, keeping the candles nearest its endTime -- so each
    successive request narrows end_ms to just before the earliest candle the
    previous page returned, until the full range is covered."""
    interval_ms = _interval_to_ms(interval)
    all_candles: list[list] = []
    cursor_end = end_ms
    for _ in range(MAX_PAGES_PER_ASSET):
        if cursor_end < start_ms:
            break
        page = await client.get_ohlcv(asset, interval, lookback_candles=0, start_ms=start_ms, end_ms=cursor_end)
        if not page:
            break
        all_candles = page + all_candles
        next_cursor_end = page[0][0] - interval_ms
        if next_cursor_end >= cursor_end:
            break  # no backward progress -- stop rather than loop forever
        cursor_end = next_cursor_end
        await asyncio.sleep(PAGE_REQUEST_DELAY_SECONDS)
    return all_candles


async def _paginated_get_funding_history(
    client: HyperliquidClient, asset: str, start_time_ms: int, end_time_ms: int,
) -> list[dict]:
    """fundingHistory takes no end_time and caps entries per response (500
    was observed for a request nominally spanning 12 months) -- walk forward
    from the last entry's own timestamp until the response is empty or has
    caught up to end_time_ms."""
    all_funding: list[dict] = []
    cursor = start_time_ms
    for _ in range(MAX_PAGES_PER_ASSET):
        if cursor >= end_time_ms:
            break
        page = await client.get_funding_history(asset, cursor)
        if not page:
            break
        all_funding.extend(page)
        last_time = page[-1].get("time")
        if last_time is None:
            break
        next_cursor = last_time + 1
        if next_cursor <= cursor:
            break  # no forward progress -- stop rather than loop forever
        cursor = next_cursor
        await asyncio.sleep(PAGE_REQUEST_DELAY_SECONDS)
    return all_funding


async def _backfill_asset_1h_and_funding(client: HyperliquidClient, asset: str, months: int) -> dict:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - months * 30 * 24 * 3600 * 1000

    candles = await _paginated_get_ohlcv(client, asset, "1h", start_ms, end_ms)
    for c in candles:
        ts, o, h, l, close, v = c
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        append_ledger_record(
            "candles_1h", {"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
                           "o": o, "h": h, "l": l, "c": close, "v": v},
            when,
        )

    funding = await _paginated_get_funding_history(client, asset, start_ms, end_ms)
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

    candles = await _paginated_get_ohlcv(client, asset, "5m", start_ms, end_ms)
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
