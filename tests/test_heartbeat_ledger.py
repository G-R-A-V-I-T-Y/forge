from datetime import datetime, timezone

from market.heartbeat import export_heartbeat_to_ledger


def _packet():
    return {
        "timestamp": "2026-07-06T12:00:00Z",
        "assets": {
            "BTC-PERP": {
                "price": 65000.0,
                "candles_5m": [[1751803200000, 64900.0, 65100.0, 64800.0, 65000.0, 12.5]],
                "funding": 0.0001,
                "open_interest": 1000000.0,
                "liq_total_usd": 50000.0,
                "liq_long_usd": 30000.0,
                "liq_short_usd": 20000.0,
            },
        },
        "cross_asset": {},
        "regime": {"regime_tag": "range_low_vol"},
    }


def test_export_writes_one_candle_record_per_asset(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    export_heartbeat_to_ledger(_packet(), ledger_dir=ledger_dir)

    path = tmp_path / "ledger" / "candles_5m" / "2026-07.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_export_writes_funding_oi_and_liquidation_records(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    export_heartbeat_to_ledger(_packet(), ledger_dir=ledger_dir)

    for kind in ("funding", "oi", "liquidations"):
        path = tmp_path / "ledger" / kind / "2026-07.jsonl"
        assert path.exists(), f"missing {kind} ledger file"


def test_export_skips_liquidations_when_data_unavailable(tmp_path):
    packet = _packet()
    packet["assets"]["BTC-PERP"]["liq_total_usd"] = None
    ledger_dir = str(tmp_path / "ledger")
    export_heartbeat_to_ledger(packet, ledger_dir=ledger_dir)

    assert not (tmp_path / "ledger" / "liquidations" / "2026-07.jsonl").exists()
