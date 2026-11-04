"""`farmcore fleet plan`: a declared fleet is planned from real temporary inventory files through
the public CLI, and a hostile or inconsistent inventory is refused before anything happens."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner, Result

from farmcore.cli import cli

REPO = Path(__file__).resolve().parents[2]
APP_IMAGE = "registry.example.test/farmcore/app@sha256:" + "a" * 64
DATABASE_IMAGE = "registry.example.test/farmcore/postgis@sha256:" + "b" * 64
TEST_MODE = ["--test-domains"]


def _declaration(slug: str = "alpha", port: int = 3401) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "slug": slug,
        "domain": f"{slug}.pilots.example.test",
        "images": {"app": APP_IMAGE, "database": DATABASE_IMAGE},
        "binding": {"host": "127.0.0.1", "port": port},
        "limits": {
            "app_memory_mib": 1024,
            "app_cpu_millis": 1000,
            "database_memory_mib": 1024,
            "database_cpu_millis": 1000,
        },
        "secrets": {
            "postgres_password": f"{slug}/postgres_password",
            "runtime_database_password": f"{slug}/runtime_database_password",
            "admin_token": f"{slug}/admin_token",
        },
    }


def _inventory(root: Path, *declarations: dict[str, Any]) -> Path:
    folder = root / "inventory"
    folder.mkdir()
    for declaration in declarations:
        (folder / f"{declaration['slug']}.yaml").write_text(yaml.safe_dump(declaration))
    return folder


def _plan(folder: Path, *options: str) -> Result:
    return CliRunner().invoke(cli, ["fleet", "plan", str(folder), *options])


def test_a_valid_inventory_plans_each_installation_with_its_own_resources(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration("beta", 3402), _declaration("alpha", 3401))

    first = _plan(folder, *TEST_MODE)
    second = _plan(folder, *TEST_MODE)

    assert first.exit_code == 0, first.output
    assert first.stdout == second.stdout
    plan = json.loads(first.stdout)
    assert plan["domain_policy"] == "test-domains"
    assert "no live state" in plan["describes"]
    alpha, beta = plan["installations"]
    assert (alpha["slug"], beta["slug"]) == ("alpha", "beta")
    assert alpha["domain"] == "alpha.pilots.example.test"
    assert alpha["binding"] == {"host": "127.0.0.1", "port": 3401}
    assert alpha["images"] == {"app": APP_IMAGE, "database": DATABASE_IMAGE}
    assert alpha["limits"]["app_memory_mib"] == 1024
    assert alpha["compose_project"] == "farmcore-fleet-alpha"
    assert beta["compose_project"] == "farmcore-fleet-beta"
    assert alpha["state_dir"] == "installations/alpha"
    assert alpha["volumes"] == [
        "farmcore-fleet-alpha_backups",
        "farmcore-fleet-alpha_pgdata",
        "farmcore-fleet-alpha_whisper-cache",
    ]
    assert not set(alpha["volumes"]) & set(beta["volumes"])
    assert alpha["network"] == "farmcore-fleet-alpha_default"
    assert alpha["secrets"] == ["admin_token", "postgres_password", "runtime_database_password"]


def _refused(folder: Path, *options: str) -> str:
    """A refusal exits 1 with no plan on stdout; returns what the operator reads."""
    result = _plan(folder, *options)
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    return result.stderr


def _yaml_inventory(root: Path, text: str, name: str = "alpha.yaml") -> Path:
    folder = root / "inventory"
    folder.mkdir()
    (folder / name).write_text(text)
    return folder


VALID_YAML = yaml.safe_dump(_declaration())

# Each is refused by the reader itself, for its own reason; none reaches the schema.
HOSTILE_YAML = {
    "python tag": ("!!python/object/apply:os.system ['id']\n", "tags"),
    "explicit tag on a value": (VALID_YAML.replace("slug: alpha", "slug: !!str alpha"), "tags"),
    "custom tag directive": ("%TAG ! tag:evil.example.test,2000:\n---\n" + VALID_YAML, "directive"),
    "yaml directive": ("%YAML 1.1\n---\n" + VALID_YAML, "directive"),
    "anchor": (VALID_YAML + "extra: &a [1]\n", "anchors"),
    "alias": ("first: [1]\nagain: *a\n", "anchors"),
    "billion laughs": ("a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n", "anchors"),
    "merge key": (VALID_YAML + "<<: {slug: beta}\n", "keys must be"),
    "second document": (VALID_YAML + "---\n" + VALID_YAML, "one document"),
    "duplicate key": (VALID_YAML + "slug: beta\n", "duplicate key"),
    "non-string key": (VALID_YAML + "1: one\n", "keys must be"),
    "complex key": (VALID_YAML + "? [a, b]\n: c\n", "keys must be"),
    "null value": (VALID_YAML.replace("slug: alpha", "slug:"), "unsupported value"),
    "timestamp value": (VALID_YAML.replace("slug: alpha", "slug: 2024-01-01"), "unsupported value"),
    "float value": (VALID_YAML.replace("port: 3401", "port: 3401.5"), "unsupported value"),
    "boolean value": (VALID_YAML.replace("port: 3401", "port: yes"), "unsupported value"),
    "sexagesimal integer": (VALID_YAML.replace("port: 3401", "port: 56:41"), "unsupported value"),
    "octal integer": (VALID_YAML.replace("port: 3401", "port: 06511"), "unsupported value"),
    "underscored integer": (VALID_YAML.replace("port: 3401", "port: 3_401"), "unsupported value"),
    "deep nesting": ("a: " + "[" * 5000, "nested"),
    "too many nodes": ("values: [" + ", ".join(["1"] * 4000) + "]\n", "too many"),
    "oversized scalar": (f"slug: {'x' * 2000}\n", "too long"),
    "oversized file": ("# " + "x" * 200_000 + "\n" + VALID_YAML, "larger"),
    "malformed": ("slug: [unterminated\n", "not valid YAML"),
    "control character": ("slug: \x07\n", "not valid YAML"),
    "empty": ("", "one document"),
    "not a mapping": ("- alpha\n", "mapping"),
}


@pytest.mark.parametrize("label", HOSTILE_YAML)
def test_hostile_yaml_is_refused_without_being_constructed(tmp_path: Path, label: str) -> None:
    text, reason = HOSTILE_YAML[label]
    message = _refused(_yaml_inventory(tmp_path, text), *TEST_MODE)
    assert "alpha.yaml" in message
    assert reason in message


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "inventory"
    folder.mkdir()
    (folder / "alpha.yaml").write_bytes(b"slug: \xff\xfe\n")
    assert "UTF-8" in _refused(folder, *TEST_MODE)


SENTINEL = "sentinel-secret-value-do-not-print"


def test_a_symlinked_declaration_is_refused_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(f"slug: {SENTINEL}\n")
    folder = tmp_path / "inventory"
    folder.mkdir()
    (folder / "alpha.yaml").symlink_to(outside)
    message = _refused(folder, *TEST_MODE)
    assert "regular file" in message
    assert SENTINEL not in message


def test_a_symlinked_inventory_folder_is_refused(tmp_path: Path) -> None:
    real = _inventory(tmp_path, _declaration())
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert "inventory folder" in _refused(link, *TEST_MODE)


def test_a_missing_inventory_folder_is_refused(tmp_path: Path) -> None:
    assert "inventory folder" in _refused(tmp_path / "absent", *TEST_MODE)


def test_an_empty_inventory_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "inventory"
    folder.mkdir()
    assert "no installations" in _refused(folder, *TEST_MODE)


def test_a_fifo_in_the_inventory_is_refused_without_blocking(tmp_path: Path) -> None:
    folder = tmp_path / "inventory"
    folder.mkdir()
    os.mkfifo(folder / "alpha.yaml")
    assert "regular file" in _refused(folder, *TEST_MODE)


@pytest.mark.parametrize(
    "name",
    ["alpha.yml", "README.md", ".DS_Store", "Beta.yaml", "alpha.yaml.bak"],
)
def test_every_other_folder_entry_is_refused(tmp_path: Path, name: str) -> None:
    folder = _inventory(tmp_path, _declaration())
    (folder / name).write_text("anything\n")
    assert "<slug>.yaml" in _refused(folder, *TEST_MODE)


def test_a_subfolder_in_the_inventory_is_refused(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration())
    (folder / "nested").mkdir()
    assert "<slug>.yaml" in _refused(folder, *TEST_MODE)


def test_a_hostile_file_name_cannot_write_to_the_terminal(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration())
    (folder / "\x1b[31mevil\n.yaml").write_text("anything\n")
    message = _refused(folder, *TEST_MODE)
    assert "\x1b" not in message
    assert "\\x1b" in message
    assert len(message.splitlines()) == 1


def test_an_inventory_with_too_many_entries_is_refused_before_reading_them(tmp_path: Path) -> None:
    folder = tmp_path / "inventory"
    folder.mkdir()
    for number in range(65):
        (folder / f"site-{number}.yaml").write_text("!!python/object:evil\n")
    message = _refused(folder, *TEST_MODE)
    assert "too many" in message
    assert "evil" not in message
    assert "site-" not in message


def _mutated(mutation: dict[str, Any]) -> dict[str, Any]:
    """The valid declaration with values replaced; a key of `a.b` reaches into a section."""
    declaration = _declaration()
    for path, value in mutation.items():
        section = declaration
        *parents, leaf = path.split(".")
        for parent in parents:
            section = section[parent]
        section[leaf] = value
    return declaration


SECRET_REF = "alpha/admin_token"

HOSTILE_VALUES = {
    "slug with traversal": ({"slug": "../alpha"}, "slug"),
    "slug with shell": ({"slug": "alpha;rm"}, "slug"),
    "slug with upper case": ({"slug": "Alpha"}, "slug"),
    "slug with underscore": ({"slug": "al_pha"}, "slug"),
    "slug with double hyphen": ({"slug": "al--pha"}, "slug"),
    "slug starting with hyphen": ({"slug": "-alpha"}, "slug"),
    "slug too long": ({"slug": "a" * 33}, "slug"),
    "slug as number": ({"slug": 12}, "slug"),
    "wildcard domain": ({"domain": "*.pilots.example.test"}, "domain"),
    "domain with port": ({"domain": "alpha.pilots.example.test:443"}, "domain"),
    "domain with scheme": ({"domain": "https://alpha.pilots.example.test"}, "domain"),
    "domain with trailing dot": ({"domain": "alpha.pilots.example.test."}, "domain"),
    "domain in upper case": ({"domain": "ALPHA.pilots.example.test"}, "domain"),
    "punycode domain": ({"domain": "xn--80ak6aa92e.example.test"}, "domain"),
    "ip address domain": ({"domain": "192.0.2.10"}, "domain"),
    "label too long": ({"domain": "a" * 64 + ".example.test"}, "domain"),
    "tagged image": ({"images.app": "registry.example.test/farmcore/app:latest"}, "images.app"),
    "untagged image": ({"images.app": "registry.example.test/farmcore/app"}, "images.app"),
    "tag and digest": ({"images.app": "farmcore/app:1@sha256:" + "a" * 64}, "images.app"),
    "short digest": ({"images.app": "farmcore/app@sha256:abc"}, "images.app"),
    "weak digest": ({"images.database": "farmcore/db@md5:" + "a" * 32}, "images.database"),
    "image option": ({"images.app": "--privileged@sha256:" + "a" * 64}, "images.app"),
    "image credentials": ({"images.app": "u:p@ghcr.io/a@sha256:" + "a" * 64}, "images.app"),
    "binding on every address": ({"binding.host": "0.0.0.0"}, "binding.host"),
    "binding on public address": ({"binding.host": "8.8.8.8"}, "binding.host"),
    "binding as hostname": ({"binding.host": "localhost"}, "binding.host"),
    "binding on link local": ({"binding.host": "169.254.169.254"}, "binding.host"),
    "binding on ipv6": ({"binding.host": "::"}, "binding.host"),
    "privileged port": ({"binding.port": 80}, "binding.port"),
    "port zero": ({"binding.port": 0}, "binding.port"),
    "port too high": ({"binding.port": 70000}, "binding.port"),
    "port as string": ({"binding.port": "3401"}, "binding.port"),
    "memory zero": ({"limits.app_memory_mib": 0}, "limits.app_memory_mib"),
    "memory negative": ({"limits.database_memory_mib": -1}, "limits.database_memory_mib"),
    "memory absurd": ({"limits.app_memory_mib": 10**9}, "limits.app_memory_mib"),
    "memory as string": ({"limits.app_memory_mib": "1g"}, "limits.app_memory_mib"),
    "cpu zero": ({"limits.app_cpu_millis": 0}, "limits.app_cpu_millis"),
    "cpu absurd": ({"limits.database_cpu_millis": 10**9}, "limits.database_cpu_millis"),
    "absolute secret": ({"secrets.admin_token": "/etc/passwd"}, "secrets.admin_token"),
    "traversing secret": ({"secrets.admin_token": "alpha/../beta/token"}, "secrets.admin_token"),
    "home secret": ({"secrets.admin_token": "~/alpha/token"}, "secrets.admin_token"),
    "url secret": ({"secrets.admin_token": "alpha/https://x"}, "secrets.admin_token"),
    "variable secret": ({"secrets.admin_token": "alpha/$TOKEN"}, "secrets.admin_token"),
    "backslash secret": ({"secrets.admin_token": "alpha\\token"}, "secrets.admin_token"),
    "hidden secret": ({"secrets.admin_token": "alpha/.token"}, "secrets.admin_token"),
    "foreign secret": ({"secrets.admin_token": "beta/admin_token"}, "secrets.admin_token"),
    "bare secret": ({"secrets.admin_token": "alpha"}, "secrets.admin_token"),
    "shared secret": ({"secrets.postgres_password": SECRET_REF}, "secrets"),
    "wrong version": ({"schema_version": 2}, "schema_version"),
    "section as text": ({"limits": "none"}, "limits"),
}


@pytest.mark.parametrize("label", HOSTILE_VALUES)
def test_a_hostile_value_is_refused_by_field(tmp_path: Path, label: str) -> None:
    mutation, field = HOSTILE_VALUES[label]
    folder = _yaml_inventory(tmp_path, yaml.safe_dump(_mutated(mutation)))
    assert f"{field}:" in _refused(folder, *TEST_MODE)


@pytest.mark.parametrize("field", ["slug", "domain", "images", "binding", "limits", "secrets"])
def test_a_missing_section_is_refused(tmp_path: Path, field: str) -> None:
    declaration = _declaration()
    del declaration[field]
    folder = _yaml_inventory(tmp_path, yaml.safe_dump(declaration))
    assert f"{field}:" in _refused(folder, *TEST_MODE)


def test_a_refusal_repeats_no_value(tmp_path: Path) -> None:
    declaration = _mutated({"secrets.admin_token": SENTINEL, "domain": SENTINEL})
    message = _refused(_yaml_inventory(tmp_path, yaml.safe_dump(declaration)), *TEST_MODE)
    assert SENTINEL not in message
    assert "secrets.admin_token:" in message
    assert "domain:" in message


UNKNOWN_KEYS = {
    "top level": ("privileged", "unknown field"),
    "compose override": ("compose_override", "unknown field"),
    "section": ("binding.network_mode", "binding: unknown field"),
    "secret section": ("secrets.telegram_bot_token", "secrets: unknown field"),
}


@pytest.mark.parametrize("label", UNKNOWN_KEYS)
def test_an_unknown_field_is_refused_by_line_without_naming_it(tmp_path: Path, label: str) -> None:
    path, expected = UNKNOWN_KEYS[label]
    text = yaml.safe_dump(_mutated({path: "enabled"}), sort_keys=False)
    message = _refused(_yaml_inventory(tmp_path, text), *TEST_MODE)
    key = path.rsplit(".", 1)[-1]
    number = next(n for n, line in enumerate(text.splitlines(), 1) if line.strip().startswith(key))
    assert f"{expected} at line {number}" in message
    assert key not in message


@pytest.mark.parametrize(
    "key",
    [SENTINEL, "ghp_" + "abcdefghijklmnopqrstuvwxyz012345", "s3cr3t", "\x1b[31mkey", "password"],
)
def test_a_secret_pasted_as_a_key_never_reaches_the_output(tmp_path: Path, key: str) -> None:
    declaration = {**_declaration(), key: "x"}
    result = _plan(_yaml_inventory(tmp_path, yaml.safe_dump(declaration)), *TEST_MODE)
    assert result.exit_code == 1
    assert key not in result.output
    assert "\x1b" not in result.output
    assert "unknown field" in result.stderr


def test_the_file_name_must_match_the_slug(tmp_path: Path) -> None:
    folder = _yaml_inventory(tmp_path, yaml.safe_dump(_declaration("beta")))
    assert "file name" in _refused(folder, *TEST_MODE)


def test_a_duplicate_port_is_refused_naming_both_files(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration("alpha", 3401), _declaration("beta", 3401))
    message = _refused(folder, *TEST_MODE)
    assert "binding.port" in message
    assert "alpha.yaml" in message
    assert "beta.yaml" in message


def test_the_same_port_on_another_address_is_still_a_duplicate(tmp_path: Path) -> None:
    other_address = _declaration("beta", 3401)
    other_address["binding"]["host"] = "127.0.0.2"
    folder = _inventory(tmp_path, _declaration("alpha", 3401), other_address)
    assert "binding.port" in _refused(folder, *TEST_MODE)


def test_a_duplicate_domain_is_refused_across_the_whole_inventory(tmp_path: Path) -> None:
    copy = _declaration("beta", 3402)
    copy["domain"] = _declaration("alpha")["domain"]
    folder = _inventory(tmp_path, _declaration("alpha", 3401), copy, _declaration("gamma", 3403))
    message = _refused(folder, *TEST_MODE)
    assert "domain" in message
    assert "already declared by alpha.yaml" in message
    assert "gamma.yaml" not in message


def test_a_duplicate_slug_is_refused_across_the_whole_inventory(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration("alpha", 3401))
    copy = _declaration("alpha", 3402)
    copy["domain"] = "other.pilots.example.test"
    (folder / "copy.yaml").write_text(yaml.safe_dump(copy))
    message = _refused(folder, *TEST_MODE)
    assert "slug: already declared by alpha.yaml" in message
    assert "copy.yaml" in message


def test_every_invalid_file_is_reported_not_only_the_first(tmp_path: Path) -> None:
    folder = _inventory(
        tmp_path, _mutated({"binding.port": 80}), _declaration("beta", 3402), _declaration("gamma")
    )
    (folder / "gamma.yaml").write_text("!!python/object:evil\n")
    (folder / "delta.yaml").write_text("slug: delta\n")
    message = _refused(folder, *TEST_MODE)
    assert "alpha.yaml: binding.port" in message
    assert "gamma.yaml" in message
    assert "delta.yaml" in message


def test_refusals_are_reported_in_a_stable_order(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _mutated({"binding.port": 80}), _declaration("beta", 80))
    assert _refused(folder, *TEST_MODE) == _refused(folder, *TEST_MODE)


def test_a_flood_of_problems_is_bounded(tmp_path: Path) -> None:
    flood = {f"unknown_{number}": "x" for number in range(100)}
    folder = _yaml_inventory(tmp_path, yaml.safe_dump({**_declaration(), **flood}))
    lines = _refused(folder, *TEST_MODE).splitlines()
    assert len(lines) <= 51
    assert "more" in lines[-1]


def _on_domain(domain: str) -> dict[str, Any]:
    return _mutated({"domain": domain})


ALLOW = ["--allow-domain-suffix", "pilots.example.org"]


def test_an_allowlisted_subdomain_plans_under_the_allowlist_policy(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _on_domain("alpha.pilots.example.org"))
    result = _plan(folder, *ALLOW)
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert plan["domain_policy"] == "allowlist"
    assert plan["installations"][0]["domain"] == "alpha.pilots.example.org"


@pytest.mark.parametrize(
    "domain",
    [
        "alpha.example.org",
        "evilpilots.example.org",
        "pilots.example.org",
        "pilots.example.org.evil.example.net",
    ],
)
def test_a_domain_outside_the_allowlist_is_refused(tmp_path: Path, domain: str) -> None:
    folder = _inventory(tmp_path, _on_domain(domain))
    assert "domain: not under an allowed suffix" in _refused(folder, *ALLOW)


def test_a_fixture_domain_needs_the_explicit_test_mode(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration())
    assert "domain: not under an allowed suffix" in _refused(folder, *ALLOW)
    assert "--test-domains" in _refused(folder, "--allow-domain-suffix", "pilots.example.test")
    assert "--test-domains" in _refused(folder, "--allow-domain-suffix", "example.test")
    assert _plan(folder, *TEST_MODE).exit_code == 0


def test_test_mode_refuses_every_domain_that_is_not_a_fixture(tmp_path: Path) -> None:
    for number, domain in enumerate(
        ["alpha.pilots.example.org", "example.test", "alpha.example.test.evil.example.org"]
    ):
        root = tmp_path / str(number)
        root.mkdir()
        assert "domain: test mode" in _refused(_inventory(root, _on_domain(domain)), *TEST_MODE)


def test_planning_needs_exactly_one_domain_policy(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _on_domain("alpha.pilots.example.org"))
    assert "--allow-domain-suffix" in _refused(folder)
    assert "not both" in _refused(folder, *ALLOW, *TEST_MODE)


@pytest.mark.parametrize("suffix", ["*.example.org", "org", "https://example.org", "EXAMPLE.org"])
def test_a_hostile_allowlist_suffix_is_refused_without_repeating_it(
    tmp_path: Path, suffix: str
) -> None:
    folder = _inventory(tmp_path, _on_domain("alpha.pilots.example.org"))
    message = _refused(folder, "--allow-domain-suffix", suffix)
    assert "--allow-domain-suffix" in message
    assert suffix not in message


# Runs the real command in its own interpreter. An audit hook sees what the process really does
# (sockets, subprocesses, opened paths), not which internal functions it calls: the first
# forbidden event ends the process with exit 99.
AUDITED = """
import os, sys

SECRETS = sys.argv[1]
FORBIDDEN = {
    "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen",
    "os.system", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "pty.spawn",
    "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink", "os.link", "os.chmod",
    "os.chown", "os.truncate", "os.utime", "shutil.copyfile", "tempfile.mkstemp",
    "tempfile.mkdtemp",
}
WRITING = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

def stop(what):
    os.write(2, f"audited: {what}\\n".encode())
    os._exit(99)

def hook(event, args):
    # urllib3 probes for IPv6 on import: a loopback bind to port 0 that carries no traffic.
    if event == "socket.bind" and args[1] == ("::1", 0):
        return
    if event in FORBIDDEN:
        stop(event)
    if event == "open":
        path, mode, flags = args
        if isinstance(flags, int) and flags & WRITING or (mode and set(mode) & set("wax+")):
            stop("open for writing")
        if isinstance(path, str) and path.startswith(SECRETS):
            stop("open of a secret")
    if event in ("os.listdir", "os.scandir") and str(args[0]).startswith(SECRETS):
        stop("listing of the secrets folder")

sys.addaudithook(hook)
exec(sys.argv[2])
from farmcore.cli import main
sys.argv = ["farmcore", *sys.argv[3:]]
main()
"""


def _audited(tmp_path: Path, folder: Path, tamper: str = "") -> subprocess.CompletedProcess[str]:
    # Nothing an installation has: no credentials, no reachable database, no Docker on PATH.
    env = {
        "PATH": str(tmp_path / "no-such-bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": "postgresql+psycopg://nobody:nobody@127.0.0.1:1/absent",
    }
    return subprocess.run(
        [
            sys.executable,
            "-c",
            AUDITED,
            str(tmp_path / "secrets"),
            tamper,
            "fleet",
            "plan",
            str(folder),
            "--test-domains",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _tree(root: Path) -> dict[str, tuple[int, int, int, str]]:
    state = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        state[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, digest)
    return state


@pytest.fixture
def fleet_with_secrets(tmp_path: Path) -> Path:
    folder = _inventory(tmp_path, _declaration("alpha", 3401), _declaration("beta", 3402))
    for slug in ("alpha", "beta"):
        for name in ("postgres_password", "runtime_database_password", "admin_token"):
            secret = tmp_path / "secrets" / slug / name
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text(SENTINEL)
    return folder


def test_planning_touches_nothing_outside_the_inventory(
    tmp_path: Path, fleet_with_secrets: Path
) -> None:
    before = _tree(tmp_path)
    result = _audited(tmp_path, fleet_with_secrets)
    assert result.returncode == 0, result.stderr
    assert _tree(tmp_path) == before
    assert SENTINEL not in result.stdout + result.stderr
    assert [item["slug"] for item in json.loads(result.stdout)["installations"]] == [
        "alpha",
        "beta",
    ]


@pytest.mark.parametrize(
    "tamper",
    [
        "import socket; socket.create_connection(('127.0.0.1', 1), timeout=0.1)",
        "import subprocess; subprocess.run(['docker', 'ps'])",
        "open(os.path.join(SECRETS, 'alpha', 'admin_token')).read()",
        "import socket; socket.socket().bind(('127.0.0.1', 0))",
        "os.listdir(SECRETS)",
        "open('created-by-plan', 'w')",
        "os.mkdir('created-by-plan')",
    ],
)
def test_the_audit_catches_each_side_effect_it_is_there_to_forbid(
    tmp_path: Path, fleet_with_secrets: Path, tamper: str
) -> None:
    """Known-bad controls: without these, the test above could pass by watching nothing."""
    assert _audited(tmp_path, fleet_with_secrets, tamper).returncode == 99


def test_planning_needs_no_secret_files_and_no_database(tmp_path: Path) -> None:
    folder = _inventory(tmp_path, _declaration())
    assert not (tmp_path / "secrets").exists()
    assert _audited(tmp_path, folder).returncode == 0


@pytest.mark.parametrize("entry_point", ["farmcore.main", "farmcore.cli"])
def test_the_web_app_and_other_commands_load_no_hosting_code(entry_point: str) -> None:
    code = (
        f"import sys, {entry_point}; "
        "print([m for m in sys.modules if m.startswith('farmcore.hosting')])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# Help is rendered for the terminal: colour splits an option's name into separately styled parts,
# and a narrow width truncates it, so the text is compared unstyled at a fixed width.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@pytest.mark.parametrize(
    "terminal",
    [{}, {"FORCE_COLOR": "1"}, {"FORCE_COLOR": "1", "GITHUB_ACTIONS": "true"}],
    ids=["plain", "colour", "ci"],
)
def test_the_fleet_commands_are_listed_in_help(terminal: dict[str, str]) -> None:
    root = CliRunner().invoke(cli, ["fleet", "--help"], env={"COLUMNS": "120", **terminal})
    assert root.exit_code == 0
    assert "plan" in _ANSI.sub("", root.stdout)
    command = CliRunner().invoke(
        cli, ["fleet", "plan", "--help"], env={"COLUMNS": "120", **terminal}
    )
    assert command.exit_code == 0
    assert "--test-domains" in _ANSI.sub("", command.stdout)


def test_the_planned_volumes_are_the_ones_compose_declares(tmp_path: Path) -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    plan = json.loads(_plan(_inventory(tmp_path, _declaration()), *TEST_MODE).stdout)
    assert plan["installations"][0]["volumes"] == [
        f"farmcore-fleet-alpha_{volume}" for volume in sorted(compose["volumes"])
    ]


def test_the_public_example_inventory_plans_in_test_mode_only() -> None:
    folder = REPO / "examples" / "fleet"
    result = _plan(folder, *TEST_MODE)
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["installations"]) == 2
    assert _plan(folder, "--allow-domain-suffix", "pilots.example.org").exit_code == 1


def _plan_document(raw: bytes) -> Result:
    with tempfile.TemporaryDirectory() as root:
        folder = Path(root)
        (folder / "alpha.yaml").write_bytes(raw)
        return _plan(folder, *TEST_MODE)


def _assert_decided(result: Result) -> None:
    """A plan or a refusal, never an unhandled exception that might print the document."""
    assert result.exit_code in (0, 1)
    assert result.exception is None or isinstance(result.exception, SystemExit)
    if result.exit_code == 1:
        assert result.stdout == ""
    else:
        json.loads(result.stdout)


YAML_SYNTAX = st.text(alphabet="abc019 -:#&*!%|>[]{},?'\"\n\t~<=.@`", max_size=120)


@settings(max_examples=200, deadline=None)
@given(raw=st.binary(max_size=200))
def test_arbitrary_bytes_end_in_a_decision(raw: bytes) -> None:
    _assert_decided(_plan_document(raw))


@settings(max_examples=300, deadline=None)
@given(text=YAML_SYNTAX)
def test_arbitrary_yaml_syntax_ends_in_a_decision(text: str) -> None:
    _assert_decided(_plan_document(text.encode()))


@settings(max_examples=200, deadline=None)
@given(prefix=YAML_SYNTAX, suffix=YAML_SYNTAX)
def test_a_damaged_valid_document_ends_in_a_decision(prefix: str, suffix: str) -> None:
    _assert_decided(_plan_document(f"{prefix}{VALID_YAML}{suffix}".encode()))


LEAVES = [
    "slug",
    "domain",
    "images.app",
    "binding.host",
    "binding.port",
    "limits.app_memory_mib",
    "secrets.admin_token",
    "unknown_field",
    "binding.unknown_field",
]


@settings(max_examples=150, deadline=None)
@given(value=st.from_regex(r"[A-Za-z0-9_./:@-]{16,48}", fullmatch=True))
def test_a_refused_key_never_reaches_the_output(value: str) -> None:
    result = _plan_document(yaml.safe_dump({**_declaration(), value: "x"}).encode())
    _assert_decided(result)
    assert result.exit_code == 1
    assert value not in result.output


@settings(max_examples=150, deadline=None)
@given(
    field=st.sampled_from(LEAVES),
    value=st.from_regex(r"[A-Za-z0-9_./:@-]{16,48}", fullmatch=True),
)
def test_a_refused_value_never_reaches_the_output(field: str, value: str) -> None:
    result = _plan_document(yaml.safe_dump(_mutated({field: value})).encode())
    _assert_decided(result)
    assert result.exit_code == 1
    assert value not in result.output


def _written_in_order(order: list[str]) -> str:
    with tempfile.TemporaryDirectory() as root:
        folder = Path(root)
        for slug in order:
            declaration = _declaration(slug, 3401 + sorted(order).index(slug))
            (folder / f"{slug}.yaml").write_text(yaml.safe_dump(declaration))
        result = _plan(folder, *TEST_MODE)
    assert result.exit_code == 0, result.output
    return result.stdout


@settings(max_examples=30, deadline=None)
@given(order=st.permutations(["gamma", "alpha", "delta", "beta"]))
def test_the_plan_does_not_depend_on_the_order_files_were_written(order: list[str]) -> None:
    assert _written_in_order(order) == _written_in_order(sorted(order))


ACCEPTED_LIMITS = {
    "binding.port": [1024, 65535],
    "limits.app_memory_mib": [256, 65536],
    "limits.database_memory_mib": [256, 65536],
    "limits.app_cpu_millis": [100, 64000],
    "limits.database_cpu_millis": [100, 64000],
    "binding.host": ["127.0.0.1", "10.0.0.1", "172.31.0.1", "192.168.0.1", "100.64.0.1"],
}
REFUSED_LIMITS = {
    "binding.port": [1023, 65536],
    "limits.app_memory_mib": [255, 65537],
    "limits.database_memory_mib": [255, 65537],
    "limits.app_cpu_millis": [99, 64001],
    "limits.database_cpu_millis": [99, 64001],
    "binding.host": ["172.32.0.1", "100.128.0.1", "100.63.255.255", "192.169.0.1", "128.0.0.1"],
}


@pytest.mark.parametrize(
    ("field", "value"), [(f, v) for f, values in ACCEPTED_LIMITS.items() for v in values]
)
def test_the_edge_of_each_range_is_accepted(tmp_path: Path, field: str, value: Any) -> None:
    result = _plan(_inventory(tmp_path, _mutated({field: value})), *TEST_MODE)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("field", "value"), [(f, v) for f, values in REFUSED_LIMITS.items() for v in values]
)
def test_just_past_each_range_is_refused(tmp_path: Path, field: str, value: Any) -> None:
    assert f"{field}:" in _refused(_inventory(tmp_path, _mutated({field: value})), *TEST_MODE)


def test_the_longest_slug_is_accepted_and_one_more_refused(tmp_path: Path) -> None:
    longest = "a" * 32
    assert _plan(_inventory(tmp_path, _declaration(longest)), *TEST_MODE).exit_code == 0
    one_more = tmp_path / "one-more"
    one_more.mkdir()
    folder = _inventory(one_more, _declaration("a" * 33))
    assert "slug:" in _refused(folder, *TEST_MODE)


def test_a_document_at_the_size_limit_is_read_and_one_byte_more_refused(tmp_path: Path) -> None:
    declaration = yaml.safe_dump(_declaration())
    limit = 16 * 1024
    folder = _inventory(tmp_path, _declaration())
    path = folder / "alpha.yaml"
    path.write_text(declaration + "#" * (limit - len(declaration)))
    assert _plan(folder, *TEST_MODE).exit_code == 0
    path.write_text(declaration + "#" * (limit - len(declaration) + 1))
    assert "larger" in _refused(folder, *TEST_MODE)


def test_an_inventory_at_the_entry_limit_is_read_and_one_more_refused(tmp_path: Path) -> None:
    declarations = [_declaration(f"site-{n}", 3401 + n) for n in range(64)]
    folder = _inventory(tmp_path, *declarations)
    assert _plan(folder, *TEST_MODE).exit_code == 0
    (folder / "site-64.yaml").write_text(yaml.safe_dump(_declaration("site-64", 3465)))
    assert "too many" in _refused(folder, *TEST_MODE)


def test_the_domain_length_limit_applies_to_the_whole_name(tmp_path: Path) -> None:
    label = "a" * 49
    too_long = ".".join([label] * 5) + ".example.test"
    folder = _inventory(tmp_path, _mutated({"domain": too_long}))
    assert "domain:" in _refused(folder, *TEST_MODE)
