"""Manages a llama-server.exe subprocess for local LLM inference.

This module exposes a module-level singleton ``server_manager`` that
forge.py starts on boot and web/app.py uses for live start/stop/restart
control. The actual HTTP client that talks to the running server lives in
llm/llama_server_client.py.

Tuned defaults (empirically verified, per task brief):
  --reasoning off      → disables think-mode (~12-20 s/decision vs 160-290 s)
  --batch-size 2048    → ~2.9x prefill speedup vs default
  --ubatch-size 1024
  --ctx-size 24576     → comfortably above real 10-11k prompt sizes

All of these are user-configurable via store/settings.py.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LlamaServerManager:
    """Thread-safe lifecycle manager for a llama-server.exe subprocess."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._port: int = 8080

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, settings: dict[str, Any]) -> bool:
        """Start the server with the given settings.

        Returns True if the server is now running (including if it was
        already running), False if it could not be started.
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                logger.info("llama-server already running (pid=%d)", self._proc.pid)
                return True
            return self._start_locked(settings)

    def stop(self) -> None:
        """Stop the running server subprocess."""
        with self._lock:
            self._stop_locked()

    def restart(self, settings: dict[str, Any]) -> bool:
        """Stop and restart with new settings. Returns True if now running."""
        with self._lock:
            self._stop_locked()
            return self._start_locked(settings)

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def port(self) -> int:
        return self._port

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._proc is None:
                return {"running": False, "pid": None, "port": self._port}
            poll = self._proc.poll()
            return {
                "running": poll is None,
                "pid": self._proc.pid,
                "returncode": poll,
                "port": self._port,
            }

    # ------------------------------------------------------------------
    # Internal helpers (call only while holding self._lock)
    # ------------------------------------------------------------------

    def _start_locked(self, settings: dict[str, Any]) -> bool:
        binary = settings.get("llama_server_binary", "")
        model = settings.get("llama_model_path", "")

        if not binary:
            logger.error(
                "llama_server_binary is not configured; cannot start local server"
            )
            return False
        if not model:
            logger.error(
                "llama_model_path is not configured; cannot start local server"
            )
            return False

        binary_path = Path(binary)
        if not binary_path.exists():
            logger.error("llama-server binary not found: %s", binary)
            return False

        model_path = Path(model)
        if not model_path.exists():
            logger.error("llama-server model not found: %s", model)
            return False

        port = int(settings.get("llama_server_port", 8080))
        ctx = int(settings.get("context_size", 24576))
        batch = int(settings.get("batch_size", 2048))
        ubatch = int(settings.get("ubatch_size", 1024))
        threads = int(settings.get("threads", 6))
        gpu_layers = int(settings.get("gpu_layers", 99))
        reasoning = bool(settings.get("reasoning", False))

        cmd = [
            str(binary_path),
            "--model", str(model_path),
            "--port", str(port),
            "--ctx-size", str(ctx),
            "--batch-size", str(batch),
            "--ubatch-size", str(ubatch),
            "--threads", str(threads),
            "--n-gpu-layers", str(gpu_layers),
            "--parallel", "1",
            "--cont-batching",
            "--flash-attn",
            "--cache-type-k", "q8_0",
            "--cache-type-v", "q8_0",
        ]
        if not reasoning:
            cmd += ["--reasoning", "off"]

        self._port = port
        logger.info("Starting llama-server: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.error("Failed to start llama-server: %s", exc)
            self._proc = None
            return False

        logger.info(
            "llama-server started (pid=%d, port=%d)", self._proc.pid, port
        )
        return True

    def _stop_locked(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return
        logger.info("Stopping llama-server (pid=%d)", self._proc.pid)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("llama-server did not stop gracefully; killing")
            self._proc.kill()
            self._proc.wait()
        self._proc = None
        logger.info("llama-server stopped")


# Module-level singleton shared by forge.py and web/app.py.
server_manager = LlamaServerManager()
