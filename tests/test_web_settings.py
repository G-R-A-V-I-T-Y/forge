"""Tests for /settings page and /api/settings, /api/local-server/* endpoints."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from store import settings as settings_store
from web.app import app


def _client(conn, llama_srv=None) -> TestClient:
    app.state.conn = conn
    if llama_srv is not None:
        app.state.llama_server = llama_srv
    elif not hasattr(app.state, "llama_server"):
        app.state.llama_server = None
    return TestClient(app)


class TestSettingsPage:
    def test_renders_ok(self, conn):
        r = _client(conn).get("/settings")
        assert r.status_code == 200
        assert "Settings" in r.text
        assert "context_size" in r.text or "Context size" in r.text

    def test_shows_server_stopped(self, conn):
        mock_srv = MagicMock()
        mock_srv.status.return_value = {"running": False, "pid": None}
        r = _client(conn, mock_srv).get("/settings")
        assert r.status_code == 200
        assert "STOPPED" in r.text


class TestApiGetSettings:
    def test_returns_defaults_when_empty(self, conn):
        r = _client(conn).get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert data["context_size"] == 24576
        assert data["reasoning"] is False
        assert isinstance(data["model_chain"], list)

    def test_returns_persisted_value(self, conn):
        settings_store.set_value(conn, "context_size", 32768)
        r = _client(conn).get("/api/settings")
        assert r.json()["context_size"] == 32768


class TestApiSaveSettings:
    def test_saves_valid_settings(self, conn):
        body = {
            "context_size": 32768,
            "batch_size": 4096,
            "ubatch_size": 2048,
            "threads": 8,
            "gpu_layers": 99,
            "llama_server_port": 8080,
            "spawn_on_startup": False,
            "reasoning": False,
        }
        r = _client(conn).post("/api/settings", json=body)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        loaded = settings_store.load_all(conn)
        assert loaded["context_size"] == 32768
        assert loaded["threads"] == 8

    def test_rejects_context_size_too_small(self, conn):
        r = _client(conn).post("/api/settings", json={"context_size": 1024})
        assert r.status_code == 422
        data = r.json()
        assert "errors" in data

    def test_rejects_invalid_integer(self, conn):
        r = _client(conn).post("/api/settings", json={"context_size": "banana"})
        assert r.status_code == 422

    def test_restarts_server_if_running(self, conn):
        mock_srv = MagicMock()
        mock_srv.is_running.return_value = True
        mock_srv.status.return_value = {"running": True, "pid": 123}

        body = {"context_size": 32768}
        r = _client(conn, mock_srv).post("/api/settings", json=body)
        assert r.status_code == 200
        mock_srv.restart.assert_called_once()

    def test_does_not_restart_if_not_running(self, conn):
        mock_srv = MagicMock()
        mock_srv.is_running.return_value = False

        r = _client(conn, mock_srv).post("/api/settings", json={"context_size": 32768})
        assert r.status_code == 200
        mock_srv.restart.assert_not_called()

    def test_persists_model_chain(self, conn):
        chain = [
            {"kind": "opencode", "model_id": "openrouter/test", "variant": None, "display_name": "Test"},
            {"kind": "llama_server", "model_id": None, "variant": None, "display_name": "Local"},
        ]
        r = _client(conn).post("/api/settings", json={"model_chain": chain})
        assert r.status_code == 200
        loaded = settings_store.get(conn, "model_chain")
        assert loaded == chain


class TestLocalServerControl:
    def test_status_no_manager(self, conn):
        app.state.llama_server = None
        r = _client(conn).get("/api/local-server/status")
        assert r.status_code == 200
        assert r.json()["running"] is False

    def test_status_running(self, conn):
        mock_srv = MagicMock()
        mock_srv.status.return_value = {"running": True, "pid": 999, "port": 8080}
        r = _client(conn, mock_srv).get("/api/local-server/status")
        assert r.status_code == 200
        assert r.json()["running"] is True
        assert r.json()["pid"] == 999

    def test_start_calls_manager(self, conn):
        mock_srv = MagicMock()
        mock_srv.start.return_value = True
        mock_srv.status.return_value = {"running": True, "pid": 42, "port": 8080}
        r = _client(conn, mock_srv).post("/api/local-server/start")
        assert r.status_code == 200
        mock_srv.start.assert_called_once()
        assert r.json()["ok"] is True

    def test_start_returns_500_on_failure(self, conn):
        mock_srv = MagicMock()
        mock_srv.start.return_value = False
        mock_srv.status.return_value = {"running": False, "pid": None}
        r = _client(conn, mock_srv).post("/api/local-server/start")
        assert r.status_code == 500

    def test_stop_calls_manager(self, conn):
        mock_srv = MagicMock()
        mock_srv.status.return_value = {"running": False, "pid": None}
        r = _client(conn, mock_srv).post("/api/local-server/stop")
        assert r.status_code == 200
        mock_srv.stop.assert_called_once()
        assert r.json()["ok"] is True

    def test_no_manager_returns_503(self, conn):
        app.state.llama_server = None
        r = _client(conn).post("/api/local-server/start")
        assert r.status_code == 503
