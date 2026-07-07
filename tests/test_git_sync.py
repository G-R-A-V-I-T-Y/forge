import subprocess
from pathlib import Path

from store.git_sync import sync_to_git


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)


def test_sync_commits_new_ledger_file(tmp_path):
    _init_repo(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "decisions.jsonl").write_text('{"a": 1}\n')

    committed = sync_to_git(tmp_path)

    assert committed is True
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "chore(ledger)" in log.stdout


def test_sync_no_changes_returns_false(tmp_path):
    _init_repo(tmp_path)
    committed = sync_to_git(tmp_path)
    assert committed is False


def test_sync_swallows_failure_on_non_git_directory(tmp_path):
    # No git repo initialized at all -> `git add` fails; must not raise.
    (tmp_path / "ledger").mkdir()
    committed = sync_to_git(tmp_path)
    assert committed is False
