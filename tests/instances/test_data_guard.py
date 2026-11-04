"""Env files (`.env*` and per-installation `farm-a.env*`, as docs/self-hosting.md names them)
never reach git; only `.env.example` is committed.

The data guard in scripts/check.sh runs in a scratch git repository with the real script; `uv`
is a stub on PATH that runs only the guard's own Python step, so nothing after the guard runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    # `uv run python` is the guard's own step and really runs; whatever follows it is stubbed.
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "run python" ]; then\n'
        f'  shift 2; exec "{sys.executable}" "$@"\n'
        "fi\n"
        "echo uv-stub\n"
    )
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


SENTINEL = "sentinel-private-host"


def test_a_refused_example_is_named_without_repeating_its_content(scratch: Path) -> None:
    _stage(scratch, "examples/fleet/alpha.yaml", f"domain: {SENTINEL}.example.org\n")
    result = _check(scratch)
    assert result.returncode == 1
    assert "examples/fleet/alpha.yaml" in result.stderr
    assert SENTINEL not in result.stdout + result.stderr


# Every spelling the fleet's strict reader accepts for the same root mapping. Each is a real
# declaration to `farmcore fleet plan`, so none may pass as an ordinary YAML file.
SPELLINGS = {
    "double-quoted keys": '"schema_version": 1\n"domain": {domain}\n',
    "single-quoted keys": "'schema_version': 1\n'domain': {domain}\n",
    "escaped key": '"schema\\x5fversion": 1\n"\\x64omain": {domain}\n',
    "explicit keys": "? schema_version\n: 1\n? domain\n: {domain}\n",
    "flow mapping": "{{schema_version: 1, domain: {domain}}}\n",
    "quoted flow mapping": '{{"schema_version": 1, "domain": "{domain}"}}\n',
    "flow mapping on two lines": "{{\n  schema_version: 1,\n  domain: {domain}\n}}\n",
    "indented block": "  schema_version: 1\n  domain: {domain}\n",
    "document marker": "---\nschema_version: 1\ndomain: {domain}\n...\n",
    "byte order mark": "﻿schema_version: 1\ndomain: {domain}\n",
}


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_declaration_is_refused_outside_the_examples_however_it_is_spelled(
    scratch: Path, spelling: str
) -> None:
    name = "deploy/alpha.yaml"
    _stage(scratch, name, SPELLINGS[spelling].format(domain=f"{SENTINEL}.example.org"))
    result = _check(scratch)
    assert result.returncode == 1
    assert "uv-stub" not in result.stdout
    assert name in result.stderr
    assert SENTINEL not in result.stdout + result.stderr


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_real_domain_is_refused_in_the_examples_however_it_is_spelled(
    scratch: Path, spelling: str
) -> None:
    name = "examples/fleet/alpha.yaml"
    _stage(scratch, name, SPELLINGS[spelling].format(domain=f"{SENTINEL}.example.org"))
    result = _check(scratch)
    assert result.returncode == 1
    assert "uv-stub" not in result.stdout
    assert name in result.stderr
    assert SENTINEL not in result.stdout + result.stderr


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_synthetic_example_is_allowed_however_it_is_spelled(scratch: Path, spelling: str) -> None:
    domain = "alpha.pilots.example.test"
    _stage(scratch, "examples/fleet/alpha.yaml", SPELLINGS[spelling].format(domain=domain))
    result = _check(scratch)
    assert "data guard" not in result.stderr
    assert "uv-stub" in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        "domain: alpha.example.test.evil.example.org\n",
        "domain: example.test\n",
        "domain: '*.pilots.example.test'\n",
        "domain: 'pasted value.example.test'\n",
        "domain: [alpha.example.test]\n",
        "domain: 12\n",
        "slug: alpha\n",
        f"domain: !!str {SENTINEL}.example.org\n",
        f"[unterminated: {SENTINEL}\n",
    ],
)
def test_an_example_that_is_not_plainly_synthetic_is_refused(scratch: Path, text: str) -> None:
    _stage(scratch, "examples/fleet/alpha.yaml", text)
    result = _check(scratch)
    assert result.returncode == 1
    assert "examples/fleet/alpha.yaml" in result.stderr
    assert SENTINEL not in result.stdout + result.stderr


def test_ordinary_yaml_with_the_same_words_elsewhere_is_allowed(scratch: Path) -> None:
    _stage(scratch, "docker-compose.yml", "x: &a {schema_version: 1}\nservices: {y: *a}\n")
    _stage(scratch, "settings.yaml", "nested:\n  schema_version: 1\n  domain: real.example.org\n")
    _stage(scratch, "not-yaml.txt", "schema_version: 1\n")
    result = _check(scratch)
    assert "data guard" not in result.stderr
    assert "uv-stub" in result.stdout


def test_the_staged_content_is_judged_not_the_working_tree(scratch: Path) -> None:
    _stage(scratch, "deploy/alpha.yaml", '"schema_version": 1\n')
    (scratch / "deploy" / "alpha.yaml").write_text("harmless: true\n")
    assert _check(scratch).returncode == 1


def test_a_declaration_the_reader_refuses_is_still_refused(scratch: Path) -> None:
    _stage(scratch, "deploy/alpha.yaml", f"schema_version: yes\ndomain: {SENTINEL}.example.org\n")
    result = _check(scratch)
    assert result.returncode == 1
    assert "deploy/alpha.yaml" in result.stderr
    assert SENTINEL not in result.stdout + result.stderr


def test_a_declaration_is_refused_whatever_the_case_of_its_extension(scratch: Path) -> None:
    _stage(scratch, "deploy/alpha.YAML", '"schema_version": 1\n')
    result = _check(scratch)
    assert result.returncode == 1
    assert "deploy/alpha.YAML" in result.stderr


def test_a_hostile_path_cannot_write_to_the_terminal(scratch: Path) -> None:
    _stage(scratch, "deploy/\x1b[31mevil.yaml", '"schema_version": 1\n')
    result = _check(scratch)
    assert result.returncode == 1
    assert "\x1b" not in result.stderr
    assert "evil.yaml" in result.stderr
