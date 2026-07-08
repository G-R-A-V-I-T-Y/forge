import json
from datetime import datetime, timezone

import pytest

from scripts.backfill_history import _paginated_get_funding_history, _paginated_get_ohlcv, backfill


class _StubClient:
    def __init__(self, candles_1h, funding, candles_5m):
        self._candles_1h = candles_1h
        self._funding = funding
        self._candles_5m = candles_5m

    async def get_ohlcv(self, asset, interval, lookback_candles=0, start_ms=None, end_ms=None):
        return self._candles_1h if interval == "1h" else self._candles_5m

    async def get_funding_history(self, asset, start_time_ms):
        return self._funding


@pytest.mark.asyncio
async def test_backfill_writes_candles_and_funding_to_ledger(tmp_path, monkeypatch):
    import store.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))

    ts_ms = int(datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    stub = _StubClient(
        candles_1h=[[ts_ms, 100.0, 101.0, 99.0, 100.5, 10.0]],
        funding=[{"time": ts_ms, "fundingRate": 0.0001}],
        candles_5m=[[ts_ms, 100.0, 100.2, 99.9, 100.1, 1.0]],
    )

    summary = await backfill(["BTC-PERP"], stub, months=1, days_5m=1)

    assert summary["BTC-PERP"]["candles_1h"] == 1
    assert summary["BTC-PERP"]["funding"] == 1
    assert summary["BTC-PERP"]["candles_5m"] == 1

    candles_1h_file = tmp_path / "ledger" / "candles_1h" / "2025-06.jsonl"
    assert candles_1h_file.exists()
    record = json.loads(candles_1h_file.read_text(encoding="utf-8").strip())
    assert record["asset"] == "BTC-PERP"
    assert record["c"] == 100.5


class _PagedOhlcvClient:
    """Simulates candleSnapshot's real single-response page cap: a request
    spanning more than PAGE_SIZE candles silently returns only the first
    PAGE_SIZE, exactly what a real 12-month "1h" backfill did (5003 rows
    back instead of ~8760) before pagination was added."""

    PAGE_SIZE = 3

    def __init__(self, interval_ms: int, all_candles: list[list]):
        self._interval_ms = interval_ms
        self._all_candles = all_candles

    async def get_ohlcv(self, asset, interval, lookback_candles=0, start_ms=None, end_ms=None):
        page = [c for c in self._all_candles if start_ms <= c[0] < end_ms]
        return page[: self.PAGE_SIZE]


@pytest.mark.asyncio
async def test_paginated_get_ohlcv_retrieves_full_range_across_multiple_pages():
    interval_ms = 3_600_000  # 1h
    start_ms = 0
    all_candles = [
        [start_ms + i * interval_ms, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(10)
    ]
    client = _PagedOhlcvClient(interval_ms, all_candles)

    result = await _paginated_get_ohlcv(client, "BTC-PERP", "1h", start_ms, start_ms + 10 * interval_ms)

    assert [c[0] for c in result] == [c[0] for c in all_candles]


class _PagedFundingClient:
    """Simulates fundingHistory's real per-response cap the same way."""

    PAGE_SIZE = 2

    def __init__(self, all_funding: list[dict]):
        self._all_funding = all_funding

    async def get_funding_history(self, asset, start_time_ms):
        page = [f for f in self._all_funding if f["time"] >= start_time_ms]
        return page[: self.PAGE_SIZE]


@pytest.mark.asyncio
async def test_paginated_get_funding_history_retrieves_full_range_across_multiple_pages():
    interval_ms = 3_600_000
    start_ms = 0
    all_funding = [
        {"time": start_ms + i * interval_ms, "fundingRate": 0.0001} for i in range(7)
    ]
    client = _PagedFundingClient(all_funding)

    result = await _paginated_get_funding_history(client, "BTC-PERP", start_ms, start_ms + 7 * interval_ms)

    assert [f["time"] for f in result] == [f["time"] for f in all_funding]


@pytest.mark.asyncio
async def test_backfill_continues_after_one_asset_fails(tmp_path, monkeypatch):
    import store.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))

    class _FailingClient:
        async def get_ohlcv(self, *a, **kw):
            raise RuntimeError("network error")

        async def get_funding_history(self, *a, **kw):
            raise RuntimeError("network error")

    summary = await backfill(["BAD-PERP"], _FailingClient(), months=1, days_5m=1)
    assert "error" in summary["BAD-PERP"]
