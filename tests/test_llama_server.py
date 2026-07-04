"""Tests for llm/llama_server.py — subprocess lifecycle management.

All subprocess.Popen calls are mocked; no real binary is invoked.
"""
import subprocess
from unittest.mock import MagicMock, patch


from llm.llama_server import LlamaServerManager


def _minimal_settings(tmp_path) -> dict:
    """Return settings with a valid (fake) binary and model path."""
    binary = tmp_path / "llama-server.exe"
    binary.write_bytes(b"fake")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fake")
    return {
        "llama_server_binary": str(binary),
        "llama_model_path": str(model),
        "llama_server_port": 8080,
        "context_size": 24576,
        "batch_size": 2048,
        "ubatch_size": 1024,
        "threads": 6,
        "gpu_layers": 99,
        "reasoning": False,
    }


def _mock_proc(pid=1234, returncode=None):
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


class TestLlamaServerManager:
    def test_initial_status_is_stopped(self):
        mgr = LlamaServerManager()
        assert mgr.is_running() is False
        assert mgr.status()["running"] is False

    def test_start_missing_binary_returns_false(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        settings["llama_server_binary"] = "/nonexistent/llama-server.exe"
        result = mgr.start(settings)
        assert result is False
        assert mgr.is_running() is False

    def test_start_missing_model_returns_false(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        settings["llama_model_path"] = "/nonexistent/model.gguf"
        result = mgr.start(settings)
        assert result is False

    def test_start_empty_binary_path_returns_false(self):
        mgr = LlamaServerManager()
        result = mgr.start({"llama_server_binary": "", "llama_model_path": ""})
        assert result is False

    def test_start_launches_subprocess(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        proc = _mock_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            result = mgr.start(settings)

        assert result is True
        assert mgr.is_running() is True
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert "--reasoning" in cmd and "off" in cmd
        assert "--ctx-size" in cmd
        assert str(settings["context_size"]) in cmd

    def test_start_with_reasoning_on_omits_reasoning_flag(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        settings["reasoning"] = True
        proc = _mock_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            mgr.start(settings)

        cmd = mock_popen.call_args.args[0]
        assert "--reasoning" not in cmd

    def test_start_is_idempotent_when_already_running(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        proc = _mock_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            mgr.start(settings)
            mgr.start(settings)  # second call should be a no-op

        assert mock_popen.call_count == 1

    def test_stop_terminates_process(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        proc = _mock_proc()

        with patch("subprocess.Popen", return_value=proc):
            mgr.start(settings)

        mgr.stop()

        proc.terminate.assert_called_once()
        assert mgr.is_running() is False

    def test_stop_when_not_running_is_safe(self):
        mgr = LlamaServerManager()
        mgr.stop()  # should not raise

    def test_restart_stops_and_restarts(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        proc1 = _mock_proc(pid=100)
        proc2 = _mock_proc(pid=200)
        procs = iter([proc1, proc2])

        with patch("subprocess.Popen", side_effect=procs):
            mgr.start(settings)
            mgr.restart(settings)

        proc1.terminate.assert_called_once()
        assert mgr.status()["pid"] == 200

    def test_status_reflects_running_state(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        proc = _mock_proc(pid=555)

        with patch("subprocess.Popen", return_value=proc):
            mgr.start(settings)

        status = mgr.status()
        assert status["running"] is True
        assert status["pid"] == 555
        assert status["port"] == 8080

    def test_popen_failure_returns_false(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)

        with patch("subprocess.Popen", side_effect=OSError("exec failed")):
            result = mgr.start(settings)

        assert result is False
        assert mgr.is_running() is False

    def test_cmd_includes_tuning_flags(self, tmp_path):
        mgr = LlamaServerManager()
        settings = _minimal_settings(tmp_path)
        settings["batch_size"] = 4096
        settings["ubatch_size"] = 2048
        settings["threads"] = 12
        proc = _mock_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            mgr.start(settings)

        cmd = mock_popen.call_args.args[0]
        assert "--batch-size" in cmd and "4096" in cmd
        assert "--ubatch-size" in cmd and "2048" in cmd
        assert "--threads" in cmd and "12" in cmd
        assert "--parallel" in cmd and "1" in cmd
        assert "--cont-batching" in cmd
        assert "--flash-attn" in cmd
