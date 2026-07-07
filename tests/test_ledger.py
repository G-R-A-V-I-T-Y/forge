import json
from datetime import datetime, timezone

from store.ledger import append_ledger_record


def test_append_creates_month_partition_file(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    when = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    append_ledger_record(
        "decisions", {"agent": "sage_turtle", "action": "wait"}, when, ledger_dir
    )

    path = tmp_path / "ledger" / "decisions" / "2026-07.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"agent": "sage_turtle", "action": "wait"}


def test_append_twice_same_month_appends_two_lines(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    when = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    append_ledger_record("decisions", {"n": 1}, when, ledger_dir)
    append_ledger_record("decisions", {"n": 2}, when, ledger_dir)

    path = tmp_path / "ledger" / "decisions" / "2026-07.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(l)["n"] for l in lines] == [1, 2]


def test_append_different_months_creates_separate_files(tmp_path):
    ledger_dir = str(tmp_path / "ledger")
    append_ledger_record(
        "candles_5m", {"m": "jun"}, datetime(2026, 6, 30, tzinfo=timezone.utc), ledger_dir
    )
    append_ledger_record(
        "candles_5m", {"m": "jul"}, datetime(2026, 7, 1, tzinfo=timezone.utc), ledger_dir
    )

    assert (tmp_path / "ledger" / "candles_5m" / "2026-06.jsonl").exists()
    assert (tmp_path / "ledger" / "candles_5m" / "2026-07.jsonl").exists()


def test_append_swallows_write_failure(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)
    # Must not raise.
    append_ledger_record("decisions", {"n": 1}, ledger_dir=str(tmp_path / "ledger"))
