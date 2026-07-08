import json
from datetime import datetime, timezone

import pytest

from scripts.backfill_history import backfill


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
