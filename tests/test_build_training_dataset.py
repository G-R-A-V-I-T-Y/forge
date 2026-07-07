from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import mkdtemp

import pandas as pd
import pytest

from scripts.build_training_dataset import (
    _load_jsonl,
    _all_jsonl_files,
    _build_row,
    _rows_from_packets,
    _compute_labels,
    build_dataset,
    horizon_label,
    DEFAULT_HORIZONS_MINUTES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_packet(ts: datetime, asset_name: str, price: float, funding: float = 0.0001,
                  extra: dict | None = None) -> dict:
    """Build a minimal heartbeat packet matching the real shape: top-level
    timestamp/assets/regime, with per-asset scalar fields nested under
    assets.<ASSET>."""
    asset_fields = {
        "price": price,
        "volume": 1000.0,
        "open_interest": 50000.0,
        "funding": funding,
        "spread": 0.01,
        **(extra or {}),
    }
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {asset_name: asset_fields},
        "cross_asset": {},
        "regime": {"regime_tag": "range_low_vol"},
    }


def _write_jsonl(data_dir: Path, filename: str, packets: list[dict]) -> Path:
    path = data_dir / filename
    with path.open("w", encoding="utf-8") as fh:
        for pkt in packets:
            fh.write(json.dumps(pkt) + "\n")
    return path


@pytest.fixture()
def tmp_data_dir():
    return Path(mkdtemp())


@pytest.fixture()
def simple_jsonl(tmp_data_dir):
    """One asset, 5-minute cadence, 10 samples (50 minutes of history) --
    long enough for the 30m horizon to have real forward windows."""
    base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    prices = [100.0, 101.0, 102.0, 103.0, 104.0, 103.0, 102.5, 104.5, 105.0, 106.0]
    fundings = [0.0001] * len(prices)
    packets = []
    for i in range(len(prices)):
        ts = base + timedelta(minutes=5 * i)
        packets.append(_make_packet(ts, "BTC", prices[i], fundings[i]))
        packets.append(_make_packet(ts, "ETH", 200.0 + i, 0.00005))
    _write_jsonl(tmp_data_dir, "2024-01-15.jsonl", packets)
    return tmp_data_dir


@pytest.fixture()
def gap_jsonl(tmp_data_dir):
    """4 timestamps, single asset (BTC), 10-minute cadence except a 30-minute
    gap between the 3rd and 4th samples (well beyond the 10-minute staleness
    threshold)."""
    base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    offsets = [0, 10, 20, 50]
    prices = [100.0, 101.0, 102.0, 103.0]
    packets = [
        _make_packet(base + timedelta(minutes=off), "BTC", price)
        for off, price in zip(offsets, prices)
    ]
    _write_jsonl(tmp_data_dir, "2024-01-15.jsonl", packets)
    return tmp_data_dir


@pytest.fixture()
def multi_day_jsonl(tmp_data_dir):
    """2 days, 3 timestamps each (5-minute cadence), 5 assets."""
    base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    assets = ["BTC", "ETH", "SOL", "DOGE", "AVAX"]
    for day in range(2):
        day_packets = []
        for j in range(3):
            ts = base + timedelta(days=day, minutes=5 * j)
            for asset in assets:
                day_packets.append(_make_packet(ts, asset, 100.0))
        _write_jsonl(tmp_data_dir, f"2024-01-{15 + day:02d}.jsonl", day_packets)
    return tmp_data_dir


# ---------------------------------------------------------------------------
# horizon_label
# ---------------------------------------------------------------------------

class TestHorizonLabel:
    def test_minutes_only(self):
        assert horizon_label(30) == "30m"

    def test_hours(self):
        assert horizon_label(120) == "2h"
        assert horizon_label(240) == "4h"
        assert horizon_label(1440) == "24h"


# ---------------------------------------------------------------------------
# _load_jsonl / _all_jsonl_files
# ---------------------------------------------------------------------------

class TestLoadJsonl:
    def test_loads_valid_jsonl(self, simple_jsonl):
        files = sorted(simple_jsonl.glob("*.jsonl"))
        records = _load_jsonl(files[0])
        assert len(records) == 20  # 10 timestamps * 2 assets

    def test_skips_malformed_json(self, tmp_data_dir):
        path = tmp_data_dir / "bad.jsonl"
        with path.open("w") as fh:
            fh.write('{"valid": true}\n')
            fh.write('not json\n')
            fh.write('{"also_valid": true}\n')
        records = _load_jsonl(path)
        assert len(records) == 2

    def test_empty_file(self, tmp_data_dir):
        path = tmp_data_dir / "empty.jsonl"
        path.touch()
        assert _load_jsonl(path) == []


class TestAllJsonlFiles:
    def test_no_filter_returns_all(self, multi_day_jsonl):
        files = _all_jsonl_files(multi_day_jsonl, None, None)
        assert len(files) == 2

    def test_start_date_filters(self, multi_day_jsonl):
        files = _all_jsonl_files(multi_day_jsonl, datetime(2024, 1, 16).date(), None)
        assert [f.stem for f in files] == ["2024-01-16"]

    def test_end_date_filters(self, multi_day_jsonl):
        files = _all_jsonl_files(multi_day_jsonl, None, datetime(2024, 1, 15).date())
        assert [f.stem for f in files] == ["2024-01-15"]

    def test_range_excludes_all(self, multi_day_jsonl):
        files = _all_jsonl_files(
            multi_day_jsonl, datetime(2024, 2, 1).date(), datetime(2024, 2, 28).date()
        )
        assert files == []


# ---------------------------------------------------------------------------
# _build_row / _rows_from_packets
# ---------------------------------------------------------------------------

class TestBuildRow:
    def test_flat_fields(self):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        pkt = _make_packet(ts, "BTC", 100.0, extra={"rsi": 60})
        row = _build_row(pkt, "BTC", pkt["assets"]["BTC"])
        assert row["timestamp"] == "2024-01-15T10:00:00Z"
        assert row["regime_tag"] == "range_low_vol"
        assert row["asset.price"] == 100.0
        assert row["asset.volume"] == 1000.0
        assert row["asset.rsi"] == 60

    def test_excludes_candle_arrays(self):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        pkt = _make_packet(ts, "BTC", 100.0, extra={"candles_5m": [[1, 2, 3, 4, 5, 6]]})
        row = _build_row(pkt, "BTC", pkt["assets"]["BTC"])
        assert "asset.candles_5m" not in row

    def test_rows_from_packets(self):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        packets = [_make_packet(ts, "BTC", 100.0), _make_packet(ts, "ETH", 200.0)]
        rows = _rows_from_packets(packets)
        assert len(rows) == 2
        assert {r["asset_key"] for r in rows} == {"BTC", "ETH"}


# ---------------------------------------------------------------------------
# _compute_labels
# ---------------------------------------------------------------------------


def _df_for(packets: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(_rows_from_packets(packets))
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values(["asset_key", "timestamp"]).reset_index(drop=True)


class TestComputeLabels:
    def test_hand_computed_return_and_funding(self):
        """Deterministic 30m-horizon check: 6 samples (T0..T5) at 5-minute
        cadence, funding=0.0001 each. At T0 (price 100), T+30m is exactly
        T5 (price 106): fwd_return should be (106-100)/100 = 0.06, and
        fwd_funding_accrued should be the sum of the 6 *future* fundings
        (T1..T5) fields (5 samples on the way, wait -- window (T0, T0+30m]
        includes T1..T5, i.e. 6 future samples if cadence is 5m and horizon
        is 30m -> T1,T2,T3,T4,T5,T6 -- adjust to exactly 6 forward samples)."""
        base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        packets = [
            _make_packet(base + timedelta(minutes=5 * i), "BTC", prices[i], funding=0.0001)
            for i in range(len(prices))
        ]
        df = _df_for(packets)
        result = _compute_labels(df, [30])

        row0 = result.iloc[0]
        assert row0["fwd_return_30m"] == pytest.approx((106.0 - 100.0) / 100.0)
        # (T0, T0+30m] = T1..T6 -> 6 future samples, each funding 0.0001
        assert row0["fwd_funding_accrued_30m"] == pytest.approx(0.0001 * 6)

    def test_columns_exist_for_each_horizon(self, simple_jsonl):
        df = _df_for(_load_jsonl(sorted(simple_jsonl.glob("*.jsonl"))[0]))
        result = _compute_labels(df, DEFAULT_HORIZONS_MINUTES)
        for h in DEFAULT_HORIZONS_MINUTES:
            label = {30: "30m", 120: "2h", 240: "4h", 1440: "24h"}[h]
            for suffix in ("return", "vol", "maxdd", "maxrunup", "funding_accrued", "stop_hit"):
                assert f"fwd_{suffix}_{label}" in result.columns

    def test_last_row_has_none_labels(self, simple_jsonl):
        df = _df_for(_load_jsonl(sorted(simple_jsonl.glob("*.jsonl"))[0]))
        result = _compute_labels(df, [30])
        last_btc = result[result["asset_key"] == "BTC"].iloc[-1]
        assert pd.isna(last_btc["fwd_return_30m"])

    def test_gap_excludes_combination(self, gap_jsonl):
        """Samples whose 30m forward window straddles the 30-minute gap
        must have null labels, but every base row is still present."""
        df = _df_for(_load_jsonl(sorted(gap_jsonl.glob("*.jsonl"))[0]))
        result = _compute_labels(df, [30])
        assert len(result) == 4  # all base rows retained

        by_offset = result.set_index(result["timestamp"])
        t0 = by_offset.iloc[0]
        t10 = by_offset.iloc[1]
        t20 = by_offset.iloc[2]

        # T0: window (T0, T0+30m] = T10, T20 only, ends 10 min short of T30
        # (== staleness threshold, not exceeding it) -> label computed.
        assert t0["fwd_return_30m"] is not None
        # T10: window would need to reach T40, but data jumps to T50 -- more
        # than the staleness threshold short -> excluded.
        assert pd.isna(t10["fwd_return_30m"])
        # T20: forward window (T20, T50] contains a 30-minute jump -> excluded.
        assert pd.isna(t20["fwd_return_30m"])

    def test_stop_hit_labels(self):
        """A price path that first breaches +5%% TP (at T10) before ever
        breaching -2%% SL. The window must still reach the full 30m horizon
        (7 samples at 5-minute cadence, T0..T30) or the combination would be
        excluded for insufficient trailing data, independent of the gap check."""
        base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        prices = [100.0, 100.5, 106.0, 94.0, 95.0, 96.0, 97.0]
        packets = [
            _make_packet(base + timedelta(minutes=5 * i), "BTC", prices[i])
            for i in range(len(prices))
        ]
        df = _df_for(packets)
        result = _compute_labels(df, [30])
        assert result.iloc[0]["fwd_stop_hit_30m"] == "tp"

    def test_maxdd_and_maxrunup(self):
        base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        prices = [100.0, 90.0, 95.0, 110.0, 105.0, 102.0, 100.0]
        packets = [
            _make_packet(base + timedelta(minutes=5 * i), "BTC", prices[i])
            for i in range(len(prices))
        ]
        df = _df_for(packets)
        result = _compute_labels(df, [30])
        row0 = result.iloc[0]
        assert row0["fwd_maxdd_30m"] == pytest.approx((90.0 - 100.0) / 100.0)
        assert row0["fwd_maxrunup_30m"] == pytest.approx((110.0 - 100.0) / 100.0)


# ---------------------------------------------------------------------------
# build_dataset (integration)
# ---------------------------------------------------------------------------

class TestBuildDataset:
    def test_basic_flow(self, simple_jsonl):
        out = simple_jsonl / "output.parquet"
        result = build_dataset(data_dir=simple_jsonl, output_path=out)
        assert len(result) > 0
        assert out.exists()
        assert "asset.price" in result.columns
        assert set(result["asset_key"].unique()) == {"BTC", "ETH"}

    def test_multi_day(self, multi_day_jsonl):
        out = multi_day_jsonl / "output.parquet"
        result = build_dataset(data_dir=multi_day_jsonl, output_path=out)
        assert len(result) == 30  # 5 assets * 3 timestamps * 2 days
        assert sorted(result["asset_key"].unique()) == ["AVAX", "BTC", "DOGE", "ETH", "SOL"]

    def test_date_range_filters_files(self, multi_day_jsonl):
        out = multi_day_jsonl / "output.parquet"
        result = build_dataset(
            data_dir=multi_day_jsonl, output_path=out,
            start_date=datetime(2024, 1, 16).date(),
        )
        assert len(result) == 15  # only the second day

    def test_custom_horizons(self, simple_jsonl):
        out = simple_jsonl / "output_custom.parquet"
        result = build_dataset(data_dir=simple_jsonl, output_path=out, horizon_minutes=[10, 20])
        assert "fwd_return_10m" in result.columns
        assert "fwd_return_20m" in result.columns
        assert "fwd_return_30m" not in result.columns

    def test_empty_dir(self, tmp_data_dir):
        result = build_dataset(data_dir=tmp_data_dir)
        assert len(result) == 0

    def test_parquet_round_trip(self, simple_jsonl):
        out = simple_jsonl / "output_rt.parquet"
        build_dataset(data_dir=simple_jsonl, output_path=out)
        rt = pd.read_parquet(out)
        assert len(rt) > 0
        assert "fwd_return_30m" in rt.columns
        assert "asset.price" in rt.columns
        assert "fwd_stop_hit_2h" in rt.columns
