"""Tests for store/settings.py — persistence, defaults, and validation."""


from store import settings as settings_store


def test_defaults_returned_when_db_is_empty(conn):
    result = settings_store.load_all(conn)
    assert result["context_size"] == 24576
    assert result["batch_size"] == 2048
    assert result["ubatch_size"] == 1024
    assert result["threads"] == 6
    assert result["reasoning"] is False
    assert result["spawn_on_startup"] is False
    assert isinstance(result["model_chain"], list)
    assert len(result["model_chain"]) == 7


def test_get_single_default(conn):
    assert settings_store.get(conn, "context_size") == 24576
    assert settings_store.get(conn, "reasoning") is False
    assert settings_store.get(conn, "nonexistent_key") is None


def test_set_value_and_get(conn):
    settings_store.set_value(conn, "context_size", 32768)
    assert settings_store.get(conn, "context_size") == 32768


def test_set_value_bool(conn):
    settings_store.set_value(conn, "spawn_on_startup", True)
    assert settings_store.get(conn, "spawn_on_startup") is True


def test_save_all_persists_multiple(conn):
    settings_store.save_all(conn, {
        "context_size": 16384,
        "batch_size": 4096,
        "threads": 12,
    })
    all_s = settings_store.load_all(conn)
    assert all_s["context_size"] == 16384
    assert all_s["batch_size"] == 4096
    assert all_s["threads"] == 12
    # Unset keys still return defaults
    assert all_s["ubatch_size"] == 1024


def test_save_all_overwrites_previous(conn):
    settings_store.set_value(conn, "context_size", 16384)
    settings_store.save_all(conn, {"context_size": 32768})
    assert settings_store.get(conn, "context_size") == 32768


def test_model_chain_roundtrip(conn):
    chain = [
        {"kind": "opencode", "model_id": "openrouter/test", "variant": None, "display_name": "Test"},
        {"kind": "llama_server", "model_id": None, "variant": None, "display_name": "Local"},
    ]
    settings_store.set_value(conn, "model_chain", chain)
    loaded = settings_store.get(conn, "model_chain")
    assert loaded == chain


def test_load_all_merges_over_defaults(conn):
    settings_store.set_value(conn, "context_size", 65536)
    result = settings_store.load_all(conn)
    assert result["context_size"] == 65536
    assert result["batch_size"] == settings_store.DEFAULTS["batch_size"]


class TestValidation:
    def test_valid_settings_returns_no_errors(self):
        errors = settings_store.validate_server_settings({
            "context_size": 24576,
            "batch_size": 2048,
            "ubatch_size": 1024,
            "threads": 6,
        })
        assert errors == []

    def test_context_size_below_minimum(self):
        errors = settings_store.validate_server_settings({"context_size": 4096})
        assert any("context_size" in e for e in errors)

    def test_context_size_non_integer(self):
        errors = settings_store.validate_server_settings({"context_size": "big"})
        assert any("context_size" in e for e in errors)

    def test_negative_batch_size(self):
        errors = settings_store.validate_server_settings({"batch_size": -1})
        assert any("batch_size" in e for e in errors)

    def test_zero_threads(self):
        errors = settings_store.validate_server_settings({"threads": 0})
        assert any("threads" in e for e in errors)
