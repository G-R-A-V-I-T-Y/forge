"""Tests for agents/agent_runner.py's log configuration.

Each agent runs as its own OS subprocess (forge.py's fleet cycle spawns
them via asyncio.gather), and llm/model_chain.py logs *why* each tier in
the fallback chain failed (timeout, non-zero exit, invalid decision shape)
via logger.warning(...). Previously those warnings only went to stderr,
which forge.py only surfaces (truncated to 300 chars) when the subprocess
exits non-zero — on a normal exit (e.g. falling through to a working
tier), the per-tier failure reasons were lost entirely, making it
impossible to tell after the fact why a higher-priority model wasn't used.
_configure_logging() adds a persistent file handler so this is diagnosable
without re-running live tests.
"""
import logging

import agents.agent_runner as agent_runner


def test_configure_logging_attaches_file_handler(tmp_path, monkeypatch):
    log_path = tmp_path / "forge.log"

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        agent_runner._configure_logging(log_path)

        file_handlers = [
            h for h in root_logger.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) == 1
        assert file_handlers[0].baseFilename == str(log_path)
    finally:
        for h in list(root_logger.handlers):
            if h not in original_handlers:
                root_logger.removeHandler(h)
                h.close()


def test_configure_logging_writes_warnings_to_file(tmp_path):
    log_path = tmp_path / "forge.log"

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        agent_runner._configure_logging(log_path)
        logging.getLogger("llm.model_chain").warning(
            "opencode tier %s timed out after %ds", "claude-sonnet-5", 60
        )
        for h in root_logger.handlers:
            h.flush()

        contents = log_path.read_text(encoding="utf-8")
        assert "opencode tier claude-sonnet-5 timed out after 60s" in contents
    finally:
        for h in list(root_logger.handlers):
            if h not in original_handlers:
                root_logger.removeHandler(h)
                h.close()
