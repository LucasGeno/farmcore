"""A bundle is data only: restore never runs a bundle's SQL as the server's superuser.

age gives a bundle confidentiality, not authenticity: anyone holding the owner's public key
(it sits in the server's environment) can encrypt a bundle of their own. These tests play that
attacker. They build bundles on their own server (a scratch database here), with a correct
manifest and SHA-256, and check that restore loads only rows, as a role that can do nothing
else: no program runs on the database server, effects stay paused, no session travels and no
row is quoted back. The object checks before any load are in `test_bundle_checks.py`.

Seams: the `farmcore restore` CLI, the database catalog, and the database server's filesystem
(`docker exec`). The pg_restore stand-in in one test is the process boundary.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from tests.instances.support import (
    Marker,
    Untouched,
    backup,
    decrypt,
    effects,
    fake_tool,
    forge_dump,
    forged_bundle,
    installation_id,
    members,
    refused,
    restore,
    same_records,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


@pytest.fixture(scope="module")
def good(shared_source, owner_key, tmp_path_factory):
    """A's real bundle, and its two members as an attacker holding the bundle sees them."""
    key, recipient = owner_key
    bundle = backup(shared_source, recipient, tmp_path_factory.mktemp("good") / "a.age")
    return bundle, members(decrypt(bundle, key))


@pytest.fixture
def target(dbs) -> tuple[str, dict[str, str]]:
    name = dbs.create()
    return name, dbs.installation(name)


def test_database_settings_in_a_forged_bundle_are_never_applied(
    good, dbs, owner_key, target, tmp_path
):
    """Every custom-format archive records its database's settings (`DATABASE PROPERTIES`,
    used only by `pg_restore --create`). A forger sets effects on and picks an id."""
    key, recipient = owner_key
    _, files = good
    name, env = target
    forged_id = "00000000-0000-4000-8000-000000000000"
    dump = forge_dump(dbs, files, tmp_path, [
        "ALTER DATABASE \"{db}\" SET farmcore.external_effects = 'enabled'",
        f"ALTER DATABASE \"{{db}}\" SET farmcore.installation_id = '{forged_id}'",
    ], create=True)  # fmt: skip
    archive = tmp_path / "with-settings.dump"
    archive.write_bytes(dump)
    listed = subprocess.run(
        ["pg_restore", "--create", "-f", "-", archive], capture_output=True, check=True
    ).stdout
    assert b"external_effects\" TO 'enabled'" in listed  # the archive really carries it

    result = restore(forged_bundle(files, dump, recipient, tmp_path / "forged.age"), key, env)

    assert result.exit_code == 0, result.output
    assert effects(env) == "external effects: paused"
    assert installation_id(dbs, name) not in (None, forged_id)


def test_planted_session_rows_never_reach_the_copy(good, dbs, owner_key, target, tmp_path):
    """A forged bundle full of session rows (one for every identity, never expiring)."""
    key, recipient = owner_key
    _, files = good
    name, env = target
    dump = forge_dump(dbs, files, tmp_path, [
        "INSERT INTO local_identity (login, password_hash) VALUES ('mallory', 'x')",
        "INSERT INTO local_session (token_hash, identity_id, csrf_hash, created_at, expires_at) "
        "SELECT md5(id::text) || md5(id::text), id, md5(id::text) || md5(id::text), now(), "
        "'infinity' FROM local_identity",
        "INSERT INTO login_bucket VALUES ('global', 0, now(), 100)",
    ])  # fmt: skip

    result = restore(forged_bundle(files, dump, recipient, tmp_path / "forged.age"), key, env)

    assert result.exit_code == 0, result.output
    assert dbs.query(name, "SELECT count(*) FROM local_session") == [(0,)]
    assert dbs.query(name, "SELECT count(*) FROM login_bucket") == [(0,)]
    assert dbs.query(name, "SELECT count(*) FROM local_identity WHERE login = 'mallory'") == [
        (1,)
    ]  # rows are data: they load, as data


def _patch_copy_statement(dump: bytes, table: str, payload: str) -> bytes:
    """Replace the archive's COPY statement for `table` with `payload`, same length (the
    custom format records offsets, so the archive stays readable)."""
    start = dump.index(f"COPY public.{table} (".encode())
    end = dump.index(b"FROM stdin;\n", start) + len(b"FROM stdin;\n")
    original = dump[start:end]
    body = payload.encode()
    assert len(body) + 4 <= len(original), "payload too long for this table's statement"
    patched = body + b"--" + b"x" * (len(original) - len(body) - 3) + b"\n"
    assert len(patched) == len(original)
    return dump[:start] + patched + dump[end:]


def test_a_forged_copy_statement_runs_only_as_the_loader_and_cannot_run_a_program(
    shared_source, good, dbs, owner_key, target, tmp_path, private_tmp
):
    """pg_restore sends each data entry's COPY statement as the archive wrote it."""
    key, recipient = owner_key
    bundle, files = good
    name, env = target
    marker = Marker(dbs.server)
    program = marker.program().replace("''", "'")
    dump = _patch_copy_statement(
        files["farmcore.dump"], "log", f"{program};COPY public.log FROM stdin;"
    )
    patched = tmp_path / "patched.dump"
    patched.write_bytes(dump)
    listed = subprocess.run(
        ["pg_restore", "-f", "-", "--data-only", "--table=log", patched],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert b"TO PROGRAM" in listed  # the archive really carries the payload
    forged = forged_bundle(files, dump, recipient, tmp_path / "forged.age")
    untouched = Untouched(dbs, shared_source["name"], name, [bundle, forged], private_tmp)

    result = restore(forged, key, env)

    refused(result, "load failed")
    assert not marker.exists()
    untouched.check()


def test_the_loader_role_can_do_nothing_but_load_rows(
    shared_source, good, dbs, owner_key, target, tmp_path, monkeypatch
):
    """Whatever SQL an archive carries runs on the loader's connection. Run such SQL there
    (a pg_restore stand-in executes it with the loader's own credentials, then loads), and
    check every statement is refused and the copy comes out clean."""
    key, _ = owner_key
    bundle, _ = good
    name, env = target
    marker = Marker(dbs.server)
    payloads = [
        marker.program().replace("''", "'"),
        "ALTER DATABASE CURRENT SET farmcore.external_effects = 'enabled'",
        "CREATE FUNCTION public.fc_pwn() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "CREATE TABLE public.fc_extra (id int)",
        "CREATE TEMP TABLE fc_tmp (id int)",
        "CREATE SCHEMA fc_side",
        f'SET ROLE "{dbs.server.superuser}"',
        "CREATE ROLE fc_attacker LOGIN",
        "SELECT pg_read_server_files_marker FROM pg_read_file('/etc/passwd') "
        "AS pg_read_server_files_marker",
        "SELECT lo_import('/etc/passwd')",
        "DELETE FROM public.farm",
        "TRUNCATE public.farm",
        "UPDATE public.farm SET name = 'pwned'",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid()",
    ]
    log = tmp_path / "loader.log"
    sql = tmp_path / "payloads.sql"
    sql.write_text("".join(p + ";\n" for p in payloads))
    real, psql = shutil.which("pg_restore"), shutil.which("psql")
    monkeypatch.setenv("PATH", fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--use-list*)
    while IFS= read -r stmt; do
      if "{psql}" -X -q -v ON_ERROR_STOP=1 -d "$PGDATABASE" -c "$stmt" >/dev/null 2>&1; then
        echo "ALLOWED $stmt" >> "{log}"
      else
        echo "refused" >> "{log}"
      fi
    done < "{sql}"
    for other in postgres template1 "{name}"; do
      if "{psql}" -X -q -d "$other" -c "SELECT 1" >/dev/null 2>&1; then
        echo "ALLOWED connect $other" >> "{log}"
      else
        echo "refused" >> "{log}"
      fi
    done
    echo "loader=$PGUSER" >> "{log}"
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
"""))  # fmt: skip
    # A database first created on PostgreSQL 14 or older keeps CREATE on `public` for PUBLIC.
    dbs.query(name, "GRANT CREATE ON SCHEMA public TO PUBLIC")

    result = restore(bundle, key, env)

    lines = log.read_text().splitlines()
    assert [ln for ln in lines if ln.startswith("ALLOWED")] == []
    assert lines.count("refused") == len(payloads) + 3  # and no other database opens
    loader = lines[-1].removeprefix("loader=")
    assert loader.startswith("farmcore_restore_") and loader != dbs.server.superuser
    assert result.exit_code == 0, result.output
    assert not marker.exists()
    assert effects(env) == "external effects: paused"
    assert same_records(dbs, name, shared_source["name"])
    assert dbs.query(name, "SELECT count(*) FROM pg_roles WHERE rolname = :r", r=loader) == [(0,)]
    assert dbs.query(name, "SELECT count(*) FROM pg_largeobject_metadata") == [(0,)]


def _orphan_row(secret: str) -> list[str]:
    """Foreign keys are dropped for the load and re-checked as the owner after it."""
    return [
        "SET session_replication_role = replica",  # the attacker's server skips the FK check
        "INSERT INTO public.log (farm_id, type, status, name, ts_start, notes) VALUES "
        f"(999999, 'spray', 'done', '{secret}', now(), '{secret}')",
    ]


def _unparsable_value(secret: str) -> list[str]:
    """PostgreSQL quotes the value it cannot parse (`invalid input syntax ...: "<value>"`),
    and the value may itself hold quotes."""
    return [
        "ALTER TABLE public.log ALTER COLUMN ts_start TYPE text",
        "INSERT INTO public.log (farm_id, type, status, name, ts_start) "
        f"SELECT id, 'spray', 'done', 'x', '{secret}' FROM public.farm LIMIT 1",
    ]


@pytest.mark.parametrize("rows", [_orphan_row, _unparsable_value], ids=["orphan", "unparsable"])
def test_rows_farmcore_cannot_accept_are_refused_never_quoted_back_and_leave_nothing(
    shared_source, good, dbs, owner_key, target, tmp_path, private_tmp, rows
):
    key, recipient = owner_key
    bundle, files = good
    name, env = target
    secret = f'fc-row-"{uuid.uuid4().hex[:8]}"-secret'
    dump = forge_dump(dbs, files, tmp_path, rows(secret))
    forged = forged_bundle(files, dump, recipient, tmp_path / "forged.age")
    untouched = Untouched(dbs, shared_source["name"], name, [bundle, forged], private_tmp)

    result = restore(forged, key, env)

    output = refused(result)
    assert output.startswith("restore: ")
    assert "fc-row-" not in output and "-secret" not in output and "999999" not in output
    untouched.check()
