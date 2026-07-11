"""Tests for R11: repo hygiene.

Verifies that the working tree stays clean after normal operation,
no root debris has accumulated, and .omo/ / stale worktrees are
gitignored.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Root debris — R11 AC1
# ---------------------------------------------------------------------------


def _root_debris_patterns() -> list[str]:
    """Return the list of known-debris filenames that R11 removed.

    Any of these reappearing at the project root is a regression
    (someone re-created an ad-hoc script in the wrong place).
    """
    return [
        "query_trades.py",
        "query_trades2.py",
        "quick_test.py",
        "test_import.py",
        "test_event_import.py",
        "test_syntax.bat",
        "test_unwrapping.py",
    ]


class TestRootDebrisAbsent:
    """None of the R11-removed root debris files have reappeared."""

    def test_no_root_debris_files(self):
        for name in _root_debris_patterns():
            assert not (REPO_ROOT / name).exists(), (
                f"Root debris file '{name}' has reappeared — delete or move "
                f"to tests/ or scripts/"
            )


# ---------------------------------------------------------------------------
# Orphan theses — R11 AC3
# ---------------------------------------------------------------------------


def _orphan_thesis_names() -> list[str]:
    """Return the list of orphan thesis filenames that R11 removed."""
    return [
        "agent_mean_reversion_2_v1.md",
        "agent_momentum_1_v1.md",
        "amber_wolf_v1.md",
        "config_test_v1.md",
        "dupe_agent_v1.md",
        "gray_finch_v1.md",
        "test_trader_v1.md",
    ]


class TestOrphanThesesAbsent:
    """None of the R11-removed orphan thesis files have reappeared."""

    def test_no_orphan_thesis_files(self):
        theses_dir = REPO_ROOT / "agents" / "theses"
        for name in _orphan_thesis_names():
            assert not (theses_dir / name).exists(), (
                f"Orphan thesis '{name}' has reappeared — delete it"
            )


# ---------------------------------------------------------------------------
# Gitignore hygiene — R11 AC2
# ---------------------------------------------------------------------------


class TestGitignoreHygiene:
    """.omo/ and .claude/worktrees/ are gitignored."""

    def test_omo_is_gitignored(self):
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".omo/" in gitignore, ".omo/ is not in .gitignore"

    def test_claude_worktrees_are_gitignored(self):
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".claude/worktrees/" in gitignore, (
            ".claude/worktrees/ is not in .gitignore"
        )


# ---------------------------------------------------------------------------
# Boot tree cleanliness — R11 AC4
# ---------------------------------------------------------------------------
#
# This test clones the repo into a temp directory, runs `python forge.py`
# briefly (one heartbeat cycle), then checks `git status` for any unexpected
# dirtiness.  It is intentionally *not* a unit test — it's an integration
# sanity gate for the pre-run checklist.
#
# NOTE: This test depends on R2.6 (deploy_spec byte-identical write skip).
# If it fails, check whether a module is writing to a tracked file during
# startup that it should not be touching.
#


def _files_to_ignore_in_status() -> set[str]:
    """Path suffixes that are expected to change during a normal boot cycle.

    - ledger/*.jsonl — appended by heartbeat/ledger capture; committed by
      the ledger_git_sync job, not a hygiene violation.
    - state/current.json — overwritten every heartbeat cycle (designed).
    """
    return {"ledger/", "state/", ".opencode/"}


@pytest.mark.skip(
    reason="Requires full forge.py boot environment "
    "(Hyperliquid/llama-server not available in CI). "
    "Run manually: python -m pytest tests/test_hygiene.py::TestBootLeavesTreeClean -v",
)
class TestBootLeavesTreeClean:
    """After forge.py boot + one heartbeat, git status is clean except for
    expected ledger/state changes."""

    def test_boot_leaves_tree_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Clone the current repo into a temp directory
            subprocess.run(
                ["git", "clone", str(REPO_ROOT), str(tmp_path)],
                check=True,
                capture_output=True,
            )

            # Simulate a heartbeat cycle by running the relevant code
            # This would need StubMarket and no external dependencies
            result = subprocess.run(
                ["python", "forge.py"],
                cwd=tmp_path,
                capture_output=True,
                timeout=60,  # Allow one heartbeat cycle + startup
            )

            # Check git status
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )

            dirty: list[str] = []
            for line in status.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                # Skip expected ledger/state changes
                if any(
                    line.endswith(suffix) or (" " + suffix.rstrip("/")) in line
                    for suffix in _files_to_ignore_in_status()
                ):
                    continue
                dirty.append(line)

            assert not dirty, (
                f"Forge.py boot left {len(dirty)} unexpected dirty path(s):\n"
                + "\n".join(dirty[:20])
            )
