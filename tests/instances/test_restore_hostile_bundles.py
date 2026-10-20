# ruff: noqa: F811 - pytest fixtures are imported from test_backup_restore and used by name
"""A bundle is data only: restore never runs a bundle's SQL as the server's superuser.

age gives a bundle confidentiality, not authenticity: anyone holding the owner's public key
(it sits in the server's environment) can encrypt a bundle of their own. These tests play that
attacker. They build bundles on their own server (a scratch database here), with a correct
manifest and SHA-256, and check that restore refuses them, or loads only rows, as a role that
can do nothing else: no program runs on the database server, effects stay paused, no session
travels and no object outside FarmCore's own schema appears.

Seams: the `farmcore restore` CLI, the database catalog, and the database server's filesystem
(`docker exec`). The pg_restore stand-in in one test is the process boundary.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from tests.instances.test_backup_restore import (  # noqa: F401 - fixtures
    AGE_KEYGEN,
    SEMANTIC,
    Untouched,
    _replace_dump,
    backup,
    cli,
    dbs,
    decrypt,
    encrypt,
    fake_tool,
    member,
    members,
    owner_key,
    pg_tools,
    private_tmp,
    refusal,
    repack,
    source,
    table_rows,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


def pg_env(server) -> dict[str, str]:
    return {
        **os.environ,
        "PGHOST": server.host,
        "PGPORT": str(server.port),
        "PGUSER": server.superuser,
        "PGPASSWORD": server.password,
    }


def forge_dump(dbs, files: dict[str, bytes], tmp_path: Path, sql: list[str], **opts) -> bytes:
    """What an attacker does on their own server: load A's dump, add `sql`, dump again."""
    scratch = dbs.create(postgis=False)
    original = tmp_path / f"orig-{scratch}.dump"
    original.write_bytes(files["farmcore.dump"])
    env = pg_env(dbs.server)
    subprocess.run(
        ["pg_restore", "--no-owner", "--no-acl", "-d", scratch, original],
        check=True, capture_output=True, env=env,
    )  # fmt: skip
    engine = create_engine(dbs.server.url(scratch))
    with engine.begin() as conn:
        for statement in sql:
            conn.exec_driver_sql(statement.replace("{db}", scratch))
    engine.dispose()
    out = tmp_path / f"forged-{scratch}.dump"
    args = ["pg_dump", "-Fc", "--no-acl", "-d", scratch, "-f", out]
    if opts.get("create"):
        args.insert(2, "--create")
    subprocess.run(args, check=True, capture_output=True, env=env)
    return out.read_bytes()


def forged_bundle(files, dump: bytes, recipient: str, path: Path) -> Path:
    return encrypt(repack(_replace_dump(files, dump)), recipient, path)


class Marker:
    """A file a hostile bundle tries to create on the database server."""

    def __init__(self, server) -> None:
        self.server = server
        self.path = f"/tmp/fc-pwned-{uuid.uuid4().hex[:12]}"

    def exists(self) -> bool:
        done = subprocess.run(
            ["docker", "exec", self.server.container, "test", "-e", self.path], capture_output=True
        )
        return done.returncode == 0

    def clear(self) -> None:
        """Forging ran the payload on the attacker's own server (here, the same container)."""
        subprocess.run(["docker", "exec", self.server.container, "rm", "-f", self.path], check=True)

    def program(self) -> str:
        return f"COPY (SELECT current_user) TO PROGRAM ''touch {self.path}''"


def _function(marker: Marker) -> str:
    return (
        "CREATE FUNCTION public.fc_pwn() RETURNS int LANGUAGE plpgsql AS $f$ BEGIN "
        f"EXECUTE '{marker.program()}'; RETURN 1; END $f$"
    )


def _trigger_function(marker: Marker) -> str:
    return (
        "CREATE FUNCTION public.fc_pwn_t() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN "
        f"EXECUTE '{marker.program()}'; RETURN NEW; END $f$"
    )


HOSTILE = {
    # The independent verifier's bundle: refreshing the view runs a program as superuser.
    "materialized view running a program": lambda m: [
        _function(m),
        "CREATE MATERIALIZED VIEW public.fc_mv AS SELECT public.fc_pwn() AS x",
    ],
    "function": lambda m: [_function(m)],
    "trigger on a FarmCore table": lambda m: [
        _trigger_function(m),
        "CREATE TRIGGER fc_pwn BEFORE INSERT ON public.farm "
        "FOR EACH ROW EXECUTE FUNCTION public.fc_pwn_t()",
    ],
    "view": lambda m: ["CREATE VIEW public.fc_v AS SELECT 1 AS x"],
    "extra table": lambda m: ["CREATE TABLE public.fc_extra (id int)"],
    "extra index": lambda m: ["CREATE INDEX fc_extra_ix ON public.farm (name)"],
    "extra schema": lambda m: ["CREATE SCHEMA fc_side", "CREATE TABLE fc_side.t (id int)"],
    "extra column": lambda m: ["ALTER TABLE public.farm ADD COLUMN fc_extra int DEFAULT 1"],
    "rule": lambda m: [
        "CREATE RULE fc_rule AS ON INSERT TO public.farm DO ALSO SELECT 1",
    ],
    "event trigger": lambda m: [
        "CREATE FUNCTION public.fc_evt() RETURNS event_trigger LANGUAGE plpgsql "
        "AS $f$ BEGIN END $f$",
        "CREATE EVENT TRIGGER fc_evt ON ddl_command_start EXECUTE FUNCTION public.fc_evt()",
    ],
}


@pytest.fixture
def attack(source, dbs, cli, owner_key, tmp_path, private_tmp):
    """A's real bundle, decrypted as an attacker with the bundle would see it, an empty target
    and `run(bundle)`: restore, then the evidence that nothing hostile happened."""
    key, recipient = owner_key
    good = backup(cli, source, recipient, tmp_path / "a.age")
    files = members(decrypt(good, key))
    target = dbs.create()
    env = dbs.installation(target)
    return key, recipient, good, files, target, env


@pytest.mark.parametrize("case", list(HOSTILE), ids=list(HOSTILE))
def test_a_forged_bundle_with_objects_outside_farmcores_schema_is_refused_before_any_load(
    attack, source, dbs, cli, tmp_path, private_tmp, case
):
    key, recipient, good, files, target, env = attack
    marker = Marker(dbs.server)
    dump = forge_dump(dbs, files, tmp_path, HOSTILE[case](marker))
    marker.clear()
    bundle = forged_bundle(files, dump, recipient, tmp_path / "forged.age")
    untouched = Untouched(dbs, source["name"], target, [good, bundle], private_tmp)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1, result.output
    assert result.output.startswith("restore: ")
    assert not marker.exists()
    untouched.check()


def test_database_settings_in_a_forged_bundle_are_never_applied(attack, source, dbs, cli, tmp_path):
    """Every custom-format archive records its database's settings (`DATABASE PROPERTIES`,
    used only by `pg_restore --create`). A forger sets effects on and picks an id; restore
    never applies either."""
    key, recipient, good, files, target, env = attack
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
    bundle = forged_bundle(files, dump, recipient, tmp_path / "forged.age")

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 0, result.output
    assert cli("effects", env=env).output.strip() == "external effects: paused"
    restored_id = result.output.strip().splitlines()[-1].removeprefix("installation ")
    assert restored_id != forged_id


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
    attack, source, dbs, cli, tmp_path, private_tmp
):
    """pg_restore sends each data entry's COPY statement as the archive wrote it."""
    key, recipient, good, files, target, env = attack
    marker = Marker(dbs.server)
    program = marker.program().replace("''", "'")
    payload = f"{program};COPY public.log FROM stdin;"
    dump = _patch_copy_statement(files["farmcore.dump"], "log", payload)
    patched = tmp_path / "patched.dump"
    patched.write_bytes(dump)
    listed = subprocess.run(
        ["pg_restore", "-f", "-", "--data-only", "--table=log", patched],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert b"TO PROGRAM" in listed  # the archive really carries the payload
    bundle = forged_bundle(files, dump, recipient, tmp_path / "forged.age")
    untouched = Untouched(dbs, source["name"], target, [good, bundle], private_tmp)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert "pg_restore failed" in result.output
    assert not marker.exists()
    untouched.check()


def test_the_loader_role_can_do_nothing_but_load_rows(
    attack, source, dbs, cli, tmp_path, monkeypatch
):
    """Whatever SQL an archive carries runs on the loader's connection. Run such SQL there
    (a pg_restore stand-in executes it with the loader's own credentials, then loads), and
    check every statement is refused and the copy comes out clean."""
    key, recipient, good, files, target, env = attack
    marker = Marker(dbs.server)
    program = marker.program().replace("''", "'")
    payloads = [
        program,
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
    fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--use-list*)
    while IFS= read -r stmt; do
      if "{psql}" -X -q -v ON_ERROR_STOP=1 -d "$PGDATABASE" -c "$stmt" >/dev/null 2>&1; then
        echo "ALLOWED $stmt" >> "{log}"
      else
        echo "refused" >> "{log}"
      fi
    done < "{sql}"
    echo "loader=$PGUSER" >> "{log}"
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    result = cli("restore", str(good), "--identity", str(key), env=env)

    lines = log.read_text().splitlines()
    assert [ln for ln in lines if ln.startswith("ALLOWED")] == []
    assert lines.count("refused") == len(payloads)
    loader = lines[-1].removeprefix("loader=")
    assert loader.startswith("farmcore_restore_") and loader != dbs.server.superuser
    assert result.exit_code == 0, result.output
    assert not marker.exists()
    assert cli("effects", env=env).output.strip() == "external effects: paused"
    assert dbs.query(target, SEMANTIC["records"]) == dbs.query(source["name"], SEMANTIC["records"])
    assert dbs.query(target, "SELECT count(*) FROM pg_roles WHERE rolname = :r", r=loader) == [(0,)]
    assert dbs.query(target, "SELECT count(*) FROM pg_largeobject_metadata") == [(0,)]


def test_planted_session_rows_never_reach_the_copy(attack, source, dbs, cli, tmp_path):
    """A forged bundle full of session rows (one for every identity, never expiring)."""
    key, recipient, good, files, target, env = attack
    dump = forge_dump(dbs, files, tmp_path, [
        "INSERT INTO local_identity (login, password_hash) VALUES ('mallory', 'x')",
        "INSERT INTO local_session (token_hash, identity_id, csrf_hash, created_at, expires_at) "
        "SELECT md5(id::text) || md5(id::text), id, md5(id::text) || md5(id::text), now(), "
        "'infinity' FROM local_identity",
        "INSERT INTO login_bucket VALUES ('global', 0, now(), 100)",
    ])  # fmt: skip
    bundle = forged_bundle(files, dump, recipient, tmp_path / "forged.age")

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 0, result.output
    assert dbs.query(target, "SELECT count(*) FROM local_session") == [(0,)]
    assert dbs.query(target, "SELECT count(*) FROM login_bucket") == [(0,)]
    assert dbs.query(target, "SELECT count(*) FROM local_identity WHERE login = 'mallory'") == [
        (1,)
    ]  # rows are data: they load, as data


# --- Archive shape: only plain files, sized by their header -------------------------------------


def tarinfo(name: str, data: bytes = b"", type_=tarfile.REGTYPE, **attrs) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = type_
    info.size = len(data) if type_ == tarfile.REGTYPE else 0
    for key, value in attrs.items():
        setattr(info, key, value)
    return info


def raw_tar(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


def sparse_bomb(files: dict[str, bytes], expands_to: int) -> bytes:
    """An old-GNU sparse `farmcore.dump`: 512 stored bytes that extract to `expands_to`."""
    manifest = files["manifest.json"]
    head = tarinfo("manifest.json", manifest).tobuf(tarfile.GNU_FORMAT) + manifest
    head += b"\0" * (-len(head) % 512)
    hdr = bytearray(512)
    hdr[0:13] = b"farmcore.dump"
    hdr[100:108] = b"0000600\0"
    hdr[108:116] = b"0000000\0"
    hdr[116:124] = b"0000000\0"
    hdr[124:136] = b"%011o\0" % 512
    hdr[136:148] = b"%011o\0" % 0
    hdr[156:157] = b"S"
    hdr[257:265] = b"ustar  \0"
    hdr[386:398] = b"%011o\0" % (expands_to - 512)
    hdr[398:410] = b"%011o\0" % 512
    hdr[483:495] = b"%011o\0" % expands_to
    hdr[148:156] = b" " * 8
    hdr[148:156] = b"%06o\0 " % sum(hdr)
    return head + bytes(hdr) + b"x" * 512 + b"\0" * 1024


def pax_sparse(files: dict[str, bytes]) -> bytes:
    """A PAX 0.1 sparse `farmcore.dump` (GNU.sparse.* keywords on a regular member)."""
    dump = files["farmcore.dump"][:512]
    info = tarinfo("farmcore.dump", dump)
    info.pax_headers = {
        "GNU.sparse.size": str(1 << 31),
        "GNU.sparse.numblocks": "1",
        "GNU.sparse.map": f"{(1 << 31) - 512},512",
    }
    return raw_tar([(tarinfo("manifest.json", files["manifest.json"]), files["manifest.json"]),
                    (info, dump)])  # fmt: skip


def huge_declared_size(files: dict[str, bytes]) -> bytes:
    """`farmcore.dump`'s header claims 64 GiB; the archive carries only 4 KiB of it."""
    manifest = files["manifest.json"]
    head = tarinfo("manifest.json", manifest).tobuf(tarfile.PAX_FORMAT) + manifest
    head += b"\0" * (-len(head) % 512)
    hdr = tarfile.TarInfo("farmcore.dump")
    hdr.size = 64 << 30
    return head + hdr.tobuf(tarfile.PAX_FORMAT) + files["farmcore.dump"][:4096]


def _shapes(files: dict[str, bytes]) -> dict[str, bytes]:
    m, d = files["manifest.json"], files["farmcore.dump"]
    regular = (tarinfo("manifest.json", m), m)
    return {
        "symlinked manifest": raw_tar([
            (tarinfo("manifest.json", type_=tarfile.SYMTYPE, linkname="/etc/passwd"), b""),
            (tarinfo("farmcore.dump", d), d),
        ]),
        "symlinked dump": raw_tar([
            regular, (tarinfo("farmcore.dump", type_=tarfile.SYMTYPE, linkname="../../x"), b""),
        ]),
        "hard-linked dump": raw_tar([
            regular,
            (tarinfo("farmcore.dump", type_=tarfile.LNKTYPE, linkname="manifest.json"), b""),
        ]),
        "directory dump": raw_tar([
            regular, (tarinfo("farmcore.dump", type_=tarfile.DIRTYPE), b""),
        ]),
        "fifo dump": raw_tar([regular, (tarinfo("farmcore.dump", type_=tarfile.FIFOTYPE), b"")]),
        "sparse dump (old GNU)": sparse_bomb(files, 2 << 30),
        "sparse dump (PAX)": pax_sparse(files),
        "dump larger than the archive": huge_declared_size(files),
        "path traversal": raw_tar([regular, (tarinfo("../farmcore.dump", d), d)]),
        "duplicate dump": raw_tar([
            regular, (tarinfo("farmcore.dump", d), d), (tarinfo("farmcore.dump", b"x"), b"x"),
        ]),
        "gzip archive": __import__("gzip").compress(repack(files)),
        "empty plaintext": b"",
    }  # fmt: skip


SHAPES = [
    "symlinked manifest", "symlinked dump", "hard-linked dump", "directory dump", "fifo dump",
    "sparse dump (old GNU)", "sparse dump (PAX)", "dump larger than the archive",
    "path traversal", "duplicate dump", "gzip archive", "empty plaintext",
]  # fmt: skip


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPES)
def test_restore_accepts_only_two_plain_files_and_never_writes_more_than_the_archive_holds(
    refusal, private_tmp, shape
):
    run, good, key, recipient, tmp_path = refusal
    plain = _shapes(members(decrypt(good, key)))[shape]
    bundle = encrypt(plain, recipient, tmp_path / "shaped.age")
    peak, stop = [0], threading.Event()

    def watch() -> None:  # the largest plaintext file restore writes at any moment
        while not stop.is_set():
            for path in private_tmp.rglob("*"):
                with contextlib.suppress(OSError):
                    peak[0] = max(peak[0], path.lstat().st_size)
            time.sleep(0.01)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        result = run(bundle)
    finally:
        stop.set()
        watcher.join()

    assert "not a FarmCore backup" in result.output
    assert peak[0] <= len(plain) + (1 << 20)


# --- Before decrypting ---------------------------------------------------------------------------


def test_restore_names_an_unreadable_key_or_bundle_instead_of_a_decryption_failure(
    refusal, tmp_path
):
    run, good, key, _, _ = refusal
    locked_key = tmp_path / "keys" / "locked.key"
    shutil.copy(key, locked_key)
    locked_key.chmod(0)
    locked_bundle = tmp_path / "locked.age"
    shutil.copy(good, locked_bundle)
    locked_bundle.chmod(0)
    try:
        key_result = run(good, locked_key)
        bundle_result = run(locked_bundle, key)
    finally:
        locked_key.chmod(0o600)
        locked_bundle.chmod(0o600)

    assert "cannot read the key file" in key_result.output
    assert "cannot read the bundle" in bundle_result.output
    assert "cannot decrypt" not in key_result.output + bundle_result.output


def test_restore_explains_a_plain_dump_from_an_older_release(refusal, tmp_path):
    run, good, key, _, _ = refusal
    old = tmp_path / "farmcore-20261001T020000Z.dump"
    old.write_bytes(members(decrypt(good, key))["farmcore.dump"])

    result = run(old)

    assert "plain pg_dump from an older release" in result.output
    assert "Upgrading" in result.output


def test_restore_says_what_age_reports_not_its_footer(refusal, tmp_path):
    run, good, _, _, _ = refusal
    other = tmp_path / "keys" / "other.key"
    subprocess.run([AGE_KEYGEN, "-o", other], check=True, capture_output=True)

    result = run(good, other)

    assert "no identity matched any of the recipients" in result.output
    assert "filippo.io" not in result.output


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"release": "999.0.0"}, "newer FarmCore release"),
        ({"modules": {"fc_unknown_app": {"version": "1.0.0", "state": "installed"}}},
         "fc_unknown_app"),
    ],
)  # fmt: skip
def test_restore_refuses_a_newer_release_or_an_app_this_release_lacks(refusal, change, message):
    run, good, key, recipient, tmp_path = refusal
    files = members(decrypt(good, key))
    manifest = json.loads(files["manifest.json"])
    if "modules" in change:
        change = {"modules": {**manifest["modules"], **change["modules"]}}
    manifest.update(change)
    plain = repack({**files, "manifest.json": json.dumps(manifest).encode()})
    bundle = encrypt(plain, recipient, tmp_path / "newer.age")

    assert message in run(bundle).output


def test_a_failed_load_reports_no_row_data(attack, source, dbs, cli, tmp_path, private_tmp):
    """A row that breaks a constraint fails the load; the message never quotes the row."""
    key, recipient, good, files, target, env = attack
    secret = f"fc-row-secret-{uuid.uuid4().hex[:8]}"
    dump = forge_dump(dbs, files, tmp_path, [
        "SET session_replication_role = replica",  # the attacker's server skips the FK check
        "INSERT INTO public.log (farm_id, type, status, name, ts_start, notes) VALUES "
        f"(999999, 'spray', 'done', '{secret}', now(), '{secret}')",
    ])  # fmt: skip
    bundle = forged_bundle(files, dump, recipient, tmp_path / "forged.age")
    untouched = Untouched(dbs, source["name"], target, [good, bundle], private_tmp)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert "restore: " in result.output and secret not in result.output
    assert "999999" not in result.output
    untouched.check()
