"""Back up an installation into one file encrypted to the owner's age key.

Seams: the `farmcore backup` CLI, the `age` CLI the owner uses to read a bundle, and external
tools on PATH (pg_dump and age stand-ins). The output checks are pure, in
`test_bundle_checks.py`.

Failure model (what these tests are for):
- a bundle readable without the owner's key, or carrying platform/operator secrets;
- a manifest that lies about the dump, or describes another moment than the dump;
- plaintext or a partial file left after a failed backup, or a password in its output.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

import pytest

from tests.instances.support import (
    HEAD,
    backup,
    decrypt,
    fake_tool,
    installation_id,
    members,
    owner_env,
    refused,
    restore,
    run_cli,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


def test_backup_writes_one_file_encrypted_to_the_owner_key_with_dump_and_manifest(
    shared_source, module_dbs, owner_key, tmp_path
):
    key, recipient = owner_key
    bundle = tmp_path / "out" / "a.age"
    bundle.parent.mkdir()
    before = datetime.now(UTC)

    backup(shared_source, recipient, bundle)

    raw = bundle.read_bytes()
    assert raw.startswith(b"age-encryption.org/v1\n")
    assert b"PGDMP" not in raw and b"Saint" not in raw and b"manifest" not in raw
    assert list(bundle.parent.iterdir()) == [bundle]  # no temp file left next to it
    files = members(decrypt(bundle, key))
    assert list(files) == ["manifest.json", "farmcore.dump"]
    dump = files["farmcore.dump"]
    assert dump.startswith(b"PGDMP")
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "farmcore-backup/1"
    assert manifest["installation_id"] == installation_id(module_dbs, shared_source["name"])
    assert manifest["release"] == version("farmcore")
    assert manifest["alembic_heads"] == [HEAD]
    assert manifest["modules"]["kpi"]["state"] == "uninstalled"
    assert manifest["modules"]["chat"]["state"] == "installed"
    created = datetime.fromisoformat(manifest["created_at"])
    assert before - timedelta(seconds=1) <= created <= datetime.now(UTC)
    assert manifest["dump"] == {
        "name": "farmcore.dump",
        "bytes": len(dump),
        "sha256": hashlib.sha256(dump).hexdigest(),
    }


def test_a_bundle_never_contains_platform_or_operator_secrets(
    shared_source, module_dbs, owner_key, tmp_path
):
    key, recipient = owner_key
    secret_env = {
        "ADMIN_TOKEN": secrets.token_hex(24),
        "TELEGRAM_BOT_TOKEN": f"123:{secrets.token_hex(16)}",
        "GEMINI_API_KEY": secrets.token_hex(20),
        "VERTEX_ACCESS_TOKEN": secrets.token_hex(20),
    }
    # Every platform and operator secret is in the environment the backup runs in.
    bundle = backup({**shared_source, **secret_env}, recipient, tmp_path / "a.age")

    plain = decrypt(bundle, key)
    files = members(plain)
    dump = tmp_path / "a.dump"
    dump.write_bytes(files["farmcore.dump"])
    sql = subprocess.run(
        ["pg_restore", "-f", "-", dump], check=True, capture_output=True, text=True
    ).stdout
    assert "CREATE TABLE public.log " in sql  # the readable form of the whole dump
    readable = sql + files["manifest.json"].decode()
    for name, value in {
        **secret_env,
        "POSTGRES_PASSWORD": module_dbs.server.password,
        "RUNTIME_DATABASE_PASSWORD": shared_source["RUNTIME_DATABASE_PASSWORD"],
    }.items():
        assert value not in readable and value.encode() not in plain, name
    # No role, password verifier or grant travels: those belong to the server. (The archive
    # records the owner's role name, never its password; restore ignores it.)
    assert "SCRAM-SHA-256$" not in readable
    assert shared_source["RUNTIME_DATABASE_ROLE"] not in readable
    assert "GRANT " not in sql and "CREATE ROLE" not in sql


def test_pg_dump_reads_the_snapshot_the_manifest_was_read_in(
    source, dbs, owner_key, tmp_path, monkeypatch
):
    """Between the manifest's read and pg_dump's start, another writer switches an app on: the
    dump still shows the moment the manifest describes."""
    import shutil

    key, recipient = owner_key
    real, psql = shutil.which("pg_dump"), shutil.which("psql")
    update = "UPDATE app_module SET state = 'installed' WHERE name = 'kpi'"
    monkeypatch.setenv("PATH", fake_tool(tmp_path / "bin", "pg_dump", f"""case "$*" in
  *--version*) exec "{real}" "$@";;
  *) "{psql}" -X -q -v ON_ERROR_STOP=1 -c "{update}" >/dev/null || exit 1
     exec "{real}" "$@";;
esac
"""))  # fmt: skip

    bundle = backup(source, recipient, tmp_path / "a.age")

    assert dbs.query(source["name"], "SELECT state FROM app_module WHERE name = 'kpi'") == [
        ("installed",)
    ]
    files = members(decrypt(bundle, key))
    dump = tmp_path / "a.dump"
    dump.write_bytes(files["farmcore.dump"])
    data = subprocess.run(
        ["pg_restore", "--data-only", "--table=app_module", "-f", "-", dump],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert re.search(r"^kpi\t(\w+)\t", data, re.M).group(1) == "uninstalled"
    assert json.loads(files["manifest.json"])["modules"]["kpi"]["state"] == "uninstalled"


def test_a_backup_that_age_fails_leaves_no_file_and_no_plaintext(
    shared_source, tmp_path, private_tmp
):
    out = tmp_path / "out"
    out.mkdir()

    result = run_cli("backup", "-r", "age1notakey", "-o", str(out / "a.age"),
                     env=owner_env(shared_source))  # fmt: skip

    output = refused(result)
    assert "age: error:" in output  # age's own error, not its footer
    assert "filippo.io" not in output
    assert list(out.iterdir()) == [] and list(private_tmp.iterdir()) == []


def test_backup_refuses_a_server_newer_than_its_pg_dump(
    shared_source, owner_key, tmp_path, monkeypatch
):
    _, recipient = owner_key
    path = fake_tool(tmp_path / "bin", "pg_dump", 'echo "pg_dump (PostgreSQL) 16.4"; exit 0\n')
    monkeypatch.setenv("PATH", path)

    result = run_cli("backup", "-r", recipient, "-o", str(tmp_path / "a"),
                     env=owner_env(shared_source))  # fmt: skip

    output = refused(result)
    assert "PostgreSQL 17" in output and "pg_dump 16" in output  # both versions named
    assert not (tmp_path / "a").exists()


def test_backup_says_which_tool_is_missing(shared_source, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    result = run_cli("backup", "-r", "age1x", "-o", str(tmp_path / "a"),
                     env=owner_env(shared_source))  # fmt: skip

    assert "`age`" in refused(result)


@pytest.mark.parametrize("command", ["backup", "restore", "effects"])
def test_an_unreachable_database_is_reported_in_one_line_without_its_password(
    shared_source, owner_key, tmp_path, pg_server, command
):
    key, recipient = owner_key
    password = secrets.token_hex(12)
    env = {"DATABASE_URL": pg_server.url("no_such_database", "nobody", password)}
    if command == "backup":
        result = run_cli("backup", "-r", recipient, "-o", str(tmp_path / "b"), env=env)
    elif command == "restore":
        result = restore(backup(shared_source, recipient, tmp_path / "a.age"), key, env)
    else:
        result = run_cli("effects", "pause", env=env)

    output = refused(result)
    assert output.startswith(f"{command}: cannot ")
    assert output.count("\n") == 1 and password not in output
