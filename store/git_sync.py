"""store/git_sync.py -- best-effort commit + push of the git-native ledger.

Runs on the same cadence as the heartbeat. Never raises, never blocks the
caller -- a failed or slow git operation must not stall the trading loop,
same defensive contract as market/heartbeat.py's append_historical(). See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 30
TRACKED_PATHS: tuple[str, ...] = ("ledger", "state")


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result


def sync_to_git(repo_root: Path, paths: tuple[str, ...] = TRACKED_PATHS) -> bool:
    """Stage and commit `paths`, then attempt to push.

    Returns True if a commit was made -- regardless of whether the push
    succeeded, since a failed push just retries next cycle: the *next*
    commit's push carries every prior unpushed commit along with it, so no
    data is lost by a transient network failure here.
    """
    committed = False
    existing = [p for p in paths if (repo_root / p).exists()]
    if not existing:
        return False
    try:
        _run(["git", "add", *existing], repo_root)
        staged = _run(["git", "diff", "--cached", "--quiet"], repo_root, check=False)
        if staged.returncode != 0:
            _run(["git", "commit", "-m", "chore(ledger): heartbeat sync"], repo_root)
            committed = True
    except Exception:
        logger.warning("git ledger commit failed", exc_info=True)
        return committed

    try:
        _run(["git", "push"], repo_root)
    except Exception:
        logger.warning("git ledger push failed (will retry next cycle)", exc_info=True)

    return committed
