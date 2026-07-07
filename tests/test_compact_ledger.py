import json
from pathlib import Path

import pandas as pd

from scripts.compact_ledger import _closed_month_files, _current_month, compact_ledger


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_closed_month_excludes_current_month(tmp_path):
    current = _current_month()
    _write_jsonl(tmp_path / "decisions" / f"{current}.jsonl", [{"n": 1}])
    _write_jsonl(tmp_path / "decisions" / "2020-01.jsonl", [{"n": 2}])

    closed = _closed_month_files(tmp_path)

    assert [p.name for p in closed] == ["2020-01.jsonl"]


def test_compact_converts_jsonl_to_parquet_and_deletes_source(tmp_path):
    _write_jsonl(
        tmp_path / "decisions" / "2020-01.jsonl",
        [{"ts": "2020-01-01T00:00:00Z", "agent": "sage_turtle", "action": "wait"}],
    )

    written = compact_ledger(tmp_path)

    assert len(written) == 1
    assert written[0].name == "2020-01.parquet"
    assert not (tmp_path / "decisions" / "2020-01.jsonl").exists()
    df = pd.read_parquet(written[0])
    assert df.iloc[0]["agent"] == "sage_turtle"


def test_compact_downsamples_old_candles_to_hourly(tmp_path):
    records = [
        {"ts": f"2020-01-01T00:{m:02d}:00Z", "asset": "BTC-PERP", "c": float(m)}
        for m in (0, 5, 10, 15)
    ]
    _write_jsonl(tmp_path / "candles_5m" / "2020-01.jsonl", records)

    written = compact_ledger(tmp_path, decay_window_months=1)

    df = pd.read_parquet(written[0])
    assert len(df) == 1  # all four 5m samples collapse into one hourly bucket


def test_compact_does_not_downsample_recent_candles(tmp_path):
    current = _current_month()
    # Force a "closed but recent" month by writing last month's data -- if
    # the test runs near a month boundary this could be flaky at the
    # granularity of "current vs not", but decay_window_months=12 makes it
    # safe regardless of which specific closed month it lands on.
    records = [
        {"ts": "2020-01-01T00:00:00Z", "asset": "BTC-PERP", "c": 1.0},
        {"ts": "2020-01-01T00:05:00Z", "asset": "BTC-PERP", "c": 2.0},
    ]
    _write_jsonl(tmp_path / "candles_5m" / "2020-01.jsonl", records)

    written = compact_ledger(tmp_path, decay_window_months=1200)  # effectively "never decay"

    df = pd.read_parquet(written[0])
    assert len(df) == 2  # no downsampling -- still within the decay window
