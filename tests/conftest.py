"""Shared pytest fixtures + import wiring for the autoware-index pipeline scripts.

The scripts under scripts/ and the site generator under site/ are plain modules,
not an installed package. This conftest puts both directories on sys.path so a
test can do e.g. `import build_envelopes` or (for site/build.py) `import build`.
"""

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SITE_DIR = REPO_ROOT / "site"

for _p in (SCRIPTS_DIR, SITE_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def git_repo(tmp_path):
    """Create a throwaway git repo with deterministic identity.

    Returns a SimpleNamespace with:
      .path   -> Path to the repo working tree
      .git(*args, check=True) -> run a git command in the repo, capture output
      .commit(msg="c") -> `git add -A && git commit`, returns the new HEAD sha
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, check=True):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")

    def commit(msg="c"):
        git("add", "-A")
        git("commit", "-q", "-m", msg)
        return git("rev-parse", "HEAD").stdout.strip()

    return SimpleNamespace(path=repo, git=git, commit=commit)
