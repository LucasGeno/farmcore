"""Per-installation env files (`farm-a.env`, as docs/self-hosting.md names them) never reach git.

The data guard in scripts/check.sh runs in a scratch git repository with the real script; `uv`
is a stub on PATH, so nothing after the guard runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "check.sh", repo / "scripts" / "check.sh")
    _git(repo, "init", "-q")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text("#!/bin/sh\necho uv-stub\n")
    stub.chmod(0o755)
    return repo


def _check(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{repo.parent / 'bin'}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "scripts/check.sh"], cwd=repo, env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize("name", ["farm-a.env", "installations/farm-b.env", ".env", ".env.prod"])
def test_the_data_guard_rejects_a_staged_env_file(scratch: Path, name: str) -> None:
    path = scratch / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("POSTGRES_PASSWORD=secret\n")
    assert _git(scratch, "add", "-f", name).returncode == 0
    result = _check(scratch)
    assert result.returncode == 1
    assert "data guard" in result.stderr and name in result.stderr


def test_the_data_guard_allows_the_env_example(scratch: Path) -> None:
    (scratch / ".env.example").write_text("POSTGRES_PASSWORD=\n")
    _git(scratch, "add", ".env.example")
    result = _check(scratch)
    assert "data guard" not in result.stderr
    assert "uv-stub" in result.stdout


@pytest.mark.parametrize("name", ["farm-a.env", "installations/farm-b.env", ".env.local"])
def test_git_ignores_env_files_but_not_the_example(name: str) -> None:
    assert _git(REPO, "check-ignore", "-q", "--no-index", name).returncode == 0
    assert _git(REPO, "check-ignore", "-q", "--no-index", ".env.example").returncode == 1
