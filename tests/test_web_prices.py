"""Tests for /api/prices and /health's heartbeat-freshness field."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from market.heartbeat import write_heartbeat
from web.app import app


def _client(conn, config) -> TestClient:
    app.state.conn = conn
    app.state.config = config
    app.state.provider = None
    return TestClient(app)


def _config(heartbeat_path: str) -> dict:
    return {
        "universe": ["SOL-PERP", "BTC-PERP"],
        "data_source": "stub",
        "desk": {"heartbeat_path": heartbeat_path, "heartbeat_interval_seconds": 300},
    }


def test_api_prices_reads_from_heartbeat_file(conn, tmp_path):
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {
            "SOL-PERP": {"price": 145.20},
            "BTC-PERP": {"price": 65000.0},
        },
        "cross_asset": {},
        "regime": {},
    })
    client = _client(conn, _config(heartbeat_path))
    resp = client.get("/api/prices")
    assert resp.status_code == 200
    assert resp.json() == {"SOL-PERP": 145.20, "BTC-PERP": 65000.0}


def test_api_prices_returns_empty_dict_when_heartbeat_missing(conn, tmp_path):
    heartbeat_path = str(tmp_path / "does_not_exist.json")
    client = _client(conn, _config(heartbeat_path))
    resp = client.get("/api/prices")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_api_prices_returns_empty_dict_when_heartbeat_stale(conn, tmp_path):
    heartbeat_path = str(tmp_path / "heartbeat.json")
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_heartbeat(heartbeat_path, {
        "timestamp": stale_ts,
        "assets": {"SOL-PERP": {"price": 145.20}},
        "cross_asset": {},
        "regime": {},
    })
    client = _client(conn, _config(heartbeat_path))
    resp = client.get("/api/prices")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_health_reports_heartbeat_age(conn, tmp_path):
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {},
        "cross_asset": {},
        "regime": {},
    })
    client = _client(conn, _config(heartbeat_path))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["heartbeat_age_seconds"] is not None
    assert body["heartbeat_age_seconds"] < 5.0


def test_health_heartbeat_age_none_when_missing(conn, tmp_path):
    heartbeat_path = str(tmp_path / "does_not_exist.json")
    client = _client(conn, _config(heartbeat_path))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["heartbeat_age_seconds"] is None
