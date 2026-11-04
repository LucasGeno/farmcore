"""Env files (`.env*` and per-installation `farm-a.env*`, as docs/self-hosting.md names them)
never reach git; only `.env.example` is committed.

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

# One name per class the guard and .gitignore must cover: `.env`, `.env.<x>`, `<name>.env`,
# `<name>.env.<x>`, the last two also below a directory.
ENV_FILES = [
    ".env",
    ".env.prod",
    "farm-a.env",
    "installations/farm-b.env",
    "farm-a.env.bak",
    "installations/farm-b.env.old",
]


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


def _stage(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    assert _git(repo, "add", "-f", name).returncode == 0


def _check(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{repo.parent / 'bin'}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "scripts/check.sh"], cwd=repo, env=env, capture_output=True, text=True
    )


def test_the_data_guard_rejects_every_staged_env_file(scratch: Path) -> None:
    for name in ENV_FILES:
        _stage(scratch, name, "POSTGRES_PASSWORD=secret\n")
    result = _check(scratch)
    assert result.returncode == 1
    assert "uv-stub" not in result.stdout
    listed = result.stderr.splitlines()
    assert [name for name in ENV_FILES if name not in listed] == []


def test_the_data_guard_allows_the_env_example(scratch: Path) -> None:
    _stage(scratch, ".env.example", "POSTGRES_PASSWORD=\n")
    result = _check(scratch)
    assert "data guard" not in result.stderr
    assert "uv-stub" in result.stdout


def test_git_ignores_env_files_but_not_the_example() -> None:
    not_ignored = [
        n for n in ENV_FILES if _git(REPO, "check-ignore", "-q", "--no-index", n).returncode
    ]
    assert not_ignored == []
    assert _git(REPO, "check-ignore", "-q", "--no-index", ".env.example").returncode == 1


DECLARATION = "schema_version: 1\nslug: alpha\ndomain: alpha.pilots.example.org\n"
FIXTURE_DECLARATION = "schema_version: 1\nslug: alpha\ndomain: alpha.pilots.example.test\n"


@pytest.mark.parametrize(
    "name",
    [
        "fleet-inventory/alpha.yaml",
        "ops/fleet-inventory/notes.txt",
        "fleet-state/secrets/alpha/admin_token",
    ],
)
def test_the_data_guard_rejects_private_fleet_storage(scratch: Path, name: str) -> None:
    _stage(scratch, name, "anything\n")
    result = _check(scratch)
    assert result.returncode == 1
    assert "uv-stub" not in result.stdout
    assert name in result.stderr


@pytest.mark.parametrize("name", ["deploy/alpha.yaml", "alpha.yml", "tests/fixtures/alpha.yaml"])
def test_the_data_guard_rejects_a_declaration_outside_the_examples(
    scratch: Path, name: str
) -> None:
    _stage(scratch, name, FIXTURE_DECLARATION)
    result = _check(scratch)
    assert result.returncode == 1
    assert "uv-stub" not in result.stdout
    assert name in result.stderr


def test_the_data_guard_rejects_a_real_domain_in_the_examples(scratch: Path) -> None:
    _stage(scratch, "examples/fleet/alpha.yaml", DECLARATION)
    result = _check(scratch)
    assert result.returncode == 1
    assert "uv-stub" not in result.stdout
    assert "examples/fleet/alpha.yaml" in result.stderr


def test_the_data_guard_allows_a_synthetic_example_and_other_yaml(scratch: Path) -> None:
    _stage(scratch, "examples/fleet/alpha.yaml", FIXTURE_DECLARATION)
    _stage(scratch, "docker-compose.yml", "services: {}\n")
    result = _check(scratch)
    assert "data guard" not in result.stderr
    assert "uv-stub" in result.stdout


def test_git_ignores_private_fleet_storage() -> None:
    for name in ["fleet-inventory/alpha.yaml", "ops/fleet-state/secrets/alpha/admin_token"]:
        assert _git(REPO, "check-ignore", "-q", "--no-index", name).returncode == 0, name
