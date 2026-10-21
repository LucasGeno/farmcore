"""Back up an installation into one encrypted file and restore it as a new, independent copy.

Seams: the `farmcore` CLI (`backup`, `restore`, `effects`, `migrate`, `seed`, `replay`, `users`),
HTTP sign-in, and the `age` CLI the owner uses to read a bundle. Every test gets its own
databases on the throwaway PostGIS server (conftest.py); keys and secrets live in temp dirs.

Failure model (what these tests are for):
- a bundle readable without the owner's key, or carrying platform/operator secrets;
- a wrong key, a modified or truncated bundle, or an incompatible release that reaches
  `pg_restore`, or a restore that touches a target holding data, the source or older bundles;
- a copy that shares the original's identity (cookies), sessions or effects: two copies both
  pushing advice, or both polling one bot token;
- a restore that stops halfway and leaves a copy the app can open with effects on;
- plaintext left on disk after a backup or restore, or credentials on a command line.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner, Result

from farmcore.settings import get_settings

AGE = shutil.which("age")
AGE_KEYGEN = shutil.which("age-keygen")
HEAD = "review_0001"  # the single supported Alembic head of this release

pytestmark = pytest.mark.skipif(
    AGE is None or AGE_KEYGEN is None, reason="needs the age CLI (age, age-keygen)"
)


def _pg_bindir() -> str | None:
    """A directory with pg_dump/pg_restore 17 or later (the throwaway server runs 17)."""
    candidates = [
        os.environ.get("PG_BINDIR", ""),
        "/Applications/Postgres.app/Contents/Versions/17/bin",
        "/Applications/Postgres.app/Contents/Versions/18/bin",
        "/usr/lib/postgresql/17/bin",
        "/usr/lib/postgresql/18/bin",
        str(Path(shutil.which("pg_dump") or "/nonexistent").parent),
    ]
    for d in candidates:
        exe = Path(d) / "pg_dump"
        if d and exe.exists():
            out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
            m = re.search(r"(\d+)\.", out)
            if m and int(m.group(1)) >= 17 and (Path(d) / "pg_restore").exists():
                return d
    return None


@pytest.fixture(autouse=True)
def pg_tools(monkeypatch) -> None:
    bindir = _pg_bindir()
    if bindir is None:
        pytest.skip("needs pg_dump/pg_restore 17+ (set PG_BINDIR)")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def cli(monkeypatch):
    """`farmcore ...` with the given environment, as an operator would run it."""

    def run(*args: str, env: dict[str, str], input: str | None = None) -> Result:
        from farmcore.cli import cli as app

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        try:
            return CliRunner().invoke(app, list(args), input=input, catch_exceptions=False)
        finally:
            get_settings.cache_clear()

    return run


@pytest.fixture
def owner_key(tmp_path) -> tuple[Path, str]:
    """The owner's age identity file (private, in a temp dir) and its public recipient."""
    key = tmp_path / "keys" / "owner.key"
    key.parent.mkdir()
    subprocess.run([AGE_KEYGEN, "-o", key], check=True, capture_output=True)
    recipient = subprocess.run(
        [AGE_KEYGEN, "-y", key], check=True, capture_output=True, text=True
    ).stdout.strip()
    return key, recipient


def decrypt(bundle: Path, key: Path) -> bytes:
    return subprocess.run([AGE, "-d", "-i", key, bundle], check=True, capture_output=True).stdout


def members(plain: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


class Databases:
    """New databases (PostGIS enabled) on the throwaway server, each with its own runtime role."""

    def __init__(self, server) -> None:
        self.server = server
        self.admin = create_engine(server.url("farmcore"), isolation_level="AUTOCOMMIT")
        self.names: list[str] = []
        self.roles: list[str] = []

    def create(self, *, postgis: bool = True, name: str | None = None) -> str:
        name = name or f"br_{uuid.uuid4().hex[:10]}"
        with self.admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        self.names.append(name)
        if postgis:
            db = create_engine(self.server.url(name))
            with db.begin() as conn:
                conn.execute(text("CREATE EXTENSION postgis"))
            db.dispose()
        return name

    def installation(self, name: str) -> dict[str, str]:
        """The owner environment for `farmcore migrate|backup|restore|effects` on `name`."""
        role = f"rt_{uuid.uuid4().hex[:10]}"
        self.roles.append(role)
        return {
            "DATABASE_URL": self.server.url(name),
            "RUNTIME_DATABASE_ROLE": role,
            "RUNTIME_DATABASE_PASSWORD": secrets.token_hex(16),
        }

    def runtime_url(self, env: dict[str, str]) -> str:
        name = env["DATABASE_URL"].rsplit("/", 1)[1]
        return self.server.url(name, env["RUNTIME_DATABASE_ROLE"], env["RUNTIME_DATABASE_PASSWORD"])

    def query(self, name: str, sql: str, **params) -> list[tuple]:
        """Run `sql` as the owner in its own committed transaction; the rows it returns."""
        db = create_engine(self.server.url(name))
        try:
            with db.begin() as conn:
                return [tuple(r) for r in conn.execute(text(sql), params)]
        finally:
            db.dispose()

    def close(self) -> None:
        with self.admin.connect() as conn:
            for name in self.names:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            for role in self.roles:
                if conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                ).first():
                    conn.execute(text(f'DROP OWNED BY "{role}"'))
                    conn.execute(text(f'DROP ROLE "{role}"'))
        self.admin.dispose()


@pytest.fixture
def dbs(pg_server) -> Iterator[Databases]:
    made = Databases(pg_server)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def source(dbs, cli) -> dict[str, str]:
    """Installation A: migrated, the open-data demo farm seeded and replayed, `kpi` off."""
    name = dbs.create()
    env = dbs.installation(name)
    for args in (("migrate",), ("seed", "--scene", "B"), ("replay", "--to", "2024-04-10 06:00")):
        result = cli(*args, env=env)
        assert result.exit_code == 0, result.output
    assert cli("apps", "--uninstall", "kpi", env=env).exit_code == 0
    return {**env, "name": name}


def installation_id(dbs: Databases, name: str) -> str | None:
    rows = dbs.query(
        name,
        "SELECT substr(cfg, 26) FROM pg_db_role_setting s "
        "JOIN pg_database d ON d.oid = s.setdatabase, unnest(s.setconfig) cfg "
        "WHERE d.datname = current_database() AND cfg LIKE 'farmcore.installation_id=%'",
    )
    return rows[0][0] if rows else None


# --- Backup ------------------------------------------------------------------------------------


def test_backup_writes_one_file_encrypted_to_the_owner_key_with_dump_and_manifest(
    source, dbs, cli, owner_key, tmp_path
):
    key, recipient = owner_key
    bundle = tmp_path / "out" / "a.farmcore-backup"
    bundle.parent.mkdir()
    before = datetime.now(UTC)

    result = cli("backup", "--recipient", recipient, "--output", str(bundle), env=source)

    assert result.exit_code == 0, result.output
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
    assert manifest["installation_id"] == installation_id(dbs, source["name"])
    assert manifest["release"] == version("farmcore")
    assert manifest["alembic_heads"] == [HEAD]
    assert manifest["modules"]["kpi"] == {"version": "0.1.0", "state": "uninstalled"}
    assert manifest["modules"]["chat"] == {"version": "0.1.0", "state": "installed"}
    created = datetime.fromisoformat(manifest["created_at"])
    assert before - timedelta(seconds=1) <= created <= datetime.now(UTC)
    assert manifest["dump"] == {
        "name": "farmcore.dump",
        "bytes": len(dump),
        "sha256": hashlib.sha256(dump).hexdigest(),
    }


def test_a_bundle_never_contains_platform_or_operator_secrets(
    source, dbs, cli, owner_key, tmp_path
):
    key, recipient = owner_key
    secret_env = {
        "ADMIN_TOKEN": secrets.token_hex(24),
        "TELEGRAM_BOT_TOKEN": f"123:{secrets.token_hex(16)}",
        "GEMINI_API_KEY": secrets.token_hex(20),
        "VERTEX_ACCESS_TOKEN": secrets.token_hex(20),
    }
    # Every platform and operator secret is in the environment the backup runs in.
    env = {**source, **secret_env}
    owner_password = dbs.server.password
    bundle = backup(cli, env, recipient, tmp_path / "a.farmcore-backup")

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
        "POSTGRES_PASSWORD": owner_password,
        "RUNTIME_DATABASE_PASSWORD": source["RUNTIME_DATABASE_PASSWORD"],
    }.items():
        assert value not in readable and value.encode() not in plain, name
    # No role, password verifier or grant travels: those belong to the server. (The archive
    # records the owner's role name, never its password; restore ignores it.)
    assert "SCRAM-SHA-256$" not in readable
    assert source["RUNTIME_DATABASE_ROLE"] not in readable
    assert "GRANT " not in sql and "CREATE ROLE" not in sql


# --- Restore into a new installation -----------------------------------------------------------

BASE = "https://testserver"
# Tables a restore changes on purpose: sessions are dropped; sign-in throttling starts afresh.
RESET_TABLES = {"local_session", "login_bucket"}


def _app(database_url: str):
    from farmcore.main import create_app
    from farmcore.settings import Settings

    return create_app(Settings(_env_file=None, database_url=database_url))


def sign_in(database_url: str, login: str, password: str) -> dict[str, str]:
    """A member session on the installation at `database_url`: its cookies, as the browser
    holds them (names carry the installation)."""
    from fastapi.testclient import TestClient

    with TestClient(_app(database_url), base_url=BASE) as c:
        page = c.get("/login")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        resp = c.post(
            "/login",
            data={"login": login, "password": password, "csrf_token": csrf},
            headers={"Origin": BASE},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        return dict(c.cookies)


def member_status(database_url: str, cookies: dict[str, str], farm_id: int = 1) -> int:
    from fastapi.testclient import TestClient

    with TestClient(_app(database_url), base_url=BASE, cookies=cookies) as c:
        return c.get(f"/notifications?farm={farm_id}", follow_redirects=False).status_code


def table_rows(dbs: Databases, name: str) -> dict[str, list[str]]:
    """Every row of every FarmCore table, as sorted JSON text (meaning, not storage order)."""
    tables = [
        t
        for (t,) in dbs.query(
            name,
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT EXISTS (SELECT 1 FROM "
            "pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e') ORDER BY 1",
        )
    ]
    return {
        t: sorted(r for (r,) in dbs.query(name, f'SELECT to_jsonb(t)::text FROM "{t}" t'))
        for t in tables
    }


# Records, their sources and provenance, and module evidence (advice, events), by meaning.
SEMANTIC = {
    "records": "SELECT l.type, l.status, l.name, l.ts_start, p.alias, pr.name, q.measure, "
    "q.value, q.unit FROM log l JOIN log_parcel lp ON lp.log_id = l.id JOIN parcel p "
    "ON p.id = lp.parcel_id JOIN quantity q ON q.log_id = l.id LEFT JOIN product pr "
    "ON pr.id = q.product_id ORDER BY 1, 2, 3, 4, 5, 6",
    "provenance": "SELECT l.name, ls.source_type, ls.external_key, ls.raw_payload::text, "
    "ls.precision_rank FROM log_source ls JOIN log l ON l.id = ls.log_id ORDER BY 1, 2, 3",
    "sources": "SELECT code, name, kind, licence FROM source ORDER BY code",
    "ext_ids": "SELECT source, key, model FROM ext_id ORDER BY 1, 2",
    "evidence": "SELECT p.alias, a.decision, a.evaluated_at, a.reasons::text FROM advice a "
    "JOIN parcel p ON p.id = a.parcel_id ORDER BY 1, 3",
    "events": "SELECT kind, res_model, occurred_at, payload::text FROM event ORDER BY 1, 3, 4",
}


@pytest.fixture
def member(source, cli) -> tuple[str, str, dict[str, str]]:
    """A farm owner on A, signed in: (login, password, A's session cookies)."""
    password = secrets.token_urlsafe(18)
    created = cli(
        "users", "create", "farmer", "--farm-id", "1", "--password-stdin",
        env={"DATABASE_URL": source["DATABASE_URL"]}, input=password + "\n",
    )  # fmt: skip
    assert created.exit_code == 0, created.output
    runtime = source["DATABASE_URL"]
    cookies = sign_in(runtime, "farmer", password)
    assert member_status(runtime, cookies) == 200
    return "farmer", password, cookies


def backup(cli, env: dict[str, str], recipient: str, path: Path) -> Path:
    result = cli("backup", "--recipient", recipient, "--output", str(path), env=env)
    assert result.exit_code == 0, result.output
    return path


def test_restore_makes_an_independent_copy_with_every_record_and_its_provenance(
    source, member, dbs, cli, owner_key, tmp_path
):
    key, recipient = owner_key
    login, password, a_cookies = member
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    c_env = dbs.installation(target)

    result = cli("restore", str(bundle), "--identity", str(key), env=c_env)

    assert result.exit_code == 0, result.output
    a_id, c_id = installation_id(dbs, source["name"]), installation_id(dbs, target)
    assert c_id is not None and uuid.UUID(c_id).version == 4 and c_id != a_id
    assert f"installation {c_id}" in result.output

    # Every row of every FarmCore table is the same, except the reset tables.
    a_rows, c_rows = table_rows(dbs, source["name"]), table_rows(dbs, target)
    assert set(a_rows) == set(c_rows)
    for table in sorted(set(a_rows) - RESET_TABLES):
        assert c_rows[table] == a_rows[table], table
    # ...and by meaning: records, sources, provenance and module evidence.
    for label, sql in SEMANTIC.items():
        assert dbs.query(target, sql) == dbs.query(source["name"], sql), label
    assert len(dbs.query(target, SEMANTIC["records"])) == 10
    assert len(dbs.query(target, SEMANTIC["sources"])) == 4
    assert len(dbs.query(target, SEMANTIC["evidence"])) == 10

    # No session of A is valid on C; the member signs in again with the same password.
    assert c_rows["local_session"] == []
    c_runtime = dbs.runtime_url(c_env)
    from farmcore.core.cookie_scope import cookie_prefix

    a_prefix, c_prefix = cookie_prefix(a_id), cookie_prefix(c_id)
    replayed = {c_prefix + k.removeprefix(a_prefix): v for k, v in a_cookies.items()}
    assert member_status(c_runtime, replayed) in (401, 403)
    assert member_status(c_runtime, sign_in(c_runtime, login, password)) == 200
    # A is untouched: its member is still signed in.
    assert member_status(source["DATABASE_URL"], a_cookies) == 200
    assert bundle.exists()


# --- External effects ---------------------------------------------------------------------------

CHAT = 4242
TOKEN = "123:fake-test-token"


@pytest.fixture
def restored(source, dbs, cli, owner_key, tmp_path) -> dict[str, str]:
    """C: A's backup restored into a new database. A's farm was bound to a Telegram chat."""
    dbs.query(
        source["name"],
        "UPDATE farm SET props = props || jsonb_build_object('chat', "
        "jsonb_build_object('telegram_chat_ids', jsonb_build_array(:chat))) "
        "WHERE id = 1 RETURNING id",
        chat=CHAT,
    )
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    result = cli("restore", str(bundle), "--identity", str(key), env=env)
    assert result.exit_code == 0, result.output
    return {**env, "name": target}


@pytest.fixture
def telegram(monkeypatch):
    """The Telegram boundary: a fake sender for the advice push."""
    from farmcore.apps.chat import push
    from farmcore.apps.chat.telegram import FakeTelegramSender

    fake = FakeTelegramSender()
    push.set_sender(fake)
    yield fake
    push.set_sender(None)


def test_a_restored_copy_pushes_no_advice_until_the_owner_enables_effects(
    source, restored, dbs, cli, telegram
):
    a, c = source, restored
    assert cli("effects", env=a).output.strip() == "external effects: enabled"
    assert cli("effects", env=c).output.strip() == "external effects: paused"

    # C's clock moves on and advice changes, but nothing reaches A's Telegram chat.
    stepped = cli("replay", "--to", "2024-04-11 06:00", env={**c, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert stepped.exit_code == 0, stepped.output
    assert telegram.sent == []
    # A (effects enabled) pushes the same pending changes: the gate, not a lack of events.
    stepped = cli("replay", "--to", "2024-04-11 06:00", env={**a, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert stepped.exit_code == 0, stepped.output
    assert [s.chat_id for s in telegram.sent] == [CHAT]
    telegram.sent.clear()

    # The runtime role (what the app and bot use) cannot switch effects on.
    runtime = {"DATABASE_URL": dbs.runtime_url(c)}
    refused = cli("effects", "enable", env=runtime)
    assert refused.exit_code == 1
    assert cli("effects", env=c).output.strip() == "external effects: paused"

    # The owner enables them; the next push goes out.
    enabled = cli("effects", "enable", env=c)
    assert enabled.exit_code == 0, enabled.output
    assert cli("effects", env=runtime).output.strip() == "external effects: enabled"
    stepped = cli("replay", "--to", "2024-04-12 06:00", env={**c, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert stepped.exit_code == 0, stepped.output
    assert [s.chat_id for s in telegram.sent] == [CHAT]

    # And pausing stops them again.
    assert cli("effects", "pause", env=c).exit_code == 0
    telegram.sent.clear()
    cli("replay", "--to", "2024-04-13 06:00", env={**c, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert telegram.sent == []


@pytest.fixture
def bot_boundaries(monkeypatch):
    """Telegram polling, the Bot API sender, Whisper and the clock's sleep, recorded."""
    from telegram.ext import Application

    from farmcore.apps.chat import commands
    from farmcore.apps.chat.transcribe import FakeTranscriber

    calls: list[str] = []

    class Sender:
        def __init__(self, token):
            calls.append("sender")

        def close(self):
            calls.append("sender closed")

    def sleep(seconds):
        calls.append("waiting")
        if calls.count("waiting") >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(commands, "HttpxTelegramSender", Sender)
    monkeypatch.setattr(commands, "WhisperTranscriber", lambda settings: FakeTranscriber())
    monkeypatch.setattr(commands, "sleep", sleep)
    monkeypatch.setattr(
        Application, "run_polling", lambda app, *, allowed_updates: calls.append("polling")
    )
    return calls


def test_effects_name_the_role_that_may_switch_them_and_fail_in_one_line(
    restored, dbs, cli, pg_server
):
    runtime = {"DATABASE_URL": dbs.runtime_url(restored)}
    refused = cli("effects", "enable", env=runtime)

    assert refused.exit_code == 1
    assert f"connected as {restored['RUNTIME_DATABASE_ROLE']}" in refused.output
    down = {"DATABASE_URL": pg_server.url("no_such_database", "nobody", "not-the-password")}
    for args in (("effects",), ("effects", "pause")):
        result = cli(*args, env=down)
        assert result.exit_code == 1, args
        assert result.output.startswith("effects: cannot read the database"), result.output
        assert result.output.count("\n") == 1 and "not-the-password" not in result.output


def test_a_paused_copy_never_polls_its_bot_token_until_effects_are_enabled(
    restored, dbs, cli, bot_boundaries
):
    runtime = {"DATABASE_URL": dbs.runtime_url(restored), "TELEGRAM_BOT_TOKEN": TOKEN}

    waiting = cli("bot", env=runtime)

    assert "polling" not in bot_boundaries and "sender" not in bot_boundaries
    assert bot_boundaries == ["waiting"] * 3
    assert "External effects are paused" in waiting.output

    assert cli("effects", "enable", env=restored).exit_code == 0
    bot_boundaries.clear()
    started = cli("bot", env=runtime)
    assert started.exit_code == 0, started.output
    assert bot_boundaries == ["sender", "polling", "sender closed"]


# --- Refusals before pg_restore ------------------------------------------------------------------


def encrypt(plain: bytes, recipient: str, path: Path) -> Path:
    """What anyone holding the owner's public key can do: encrypt a bundle of their own."""
    subprocess.run([AGE, "-r", recipient, "-o", path], input=plain, check=True)
    return path


def repack(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


DELETE = object()


def edit_manifest(**changes):
    """Change manifest fields (`dump__sha256` is manifest["dump"]["sha256"])."""

    def edit(files: dict[str, bytes]) -> dict[str, bytes]:
        manifest = json.loads(files["manifest.json"])
        for dotted, value in changes.items():
            *path, last = dotted.split("__")
            node = manifest
            for part in path:
                node = node[part]
            if value is DELETE:
                del node[last]
            else:
                node[last] = value
        return {**files, "manifest.json": json.dumps(manifest).encode()}

    return edit


TAMPERED = {
    "dump hash": edit_manifest(dump__sha256="0" * 64),
    "dump size": edit_manifest(dump__bytes=1),
    "dump name": edit_manifest(dump__name="../../etc/passwd"),
    "schema revision": edit_manifest(alembic_heads=[HEAD, "other_0001"]),
    "no schema revision": edit_manifest(alembic_heads=[]),
    "format": edit_manifest(format="farmcore-backup/99"),
    "missing field": edit_manifest(release=DELETE),
    "wrong type": edit_manifest(modules=["chat"]),
    "not a manifest": lambda f: {**f, "manifest.json": b"\x00not json"},
    "manifest not an object": lambda f: {**f, "manifest.json": b"[1, 2]"},
    "extra member": lambda f: {**f, "extra.sh": b"#!/bin/sh\n"},
    "members reordered": lambda f: {k: f[k] for k in ("farmcore.dump", "manifest.json")},
    "dump replaced": lambda f: {**f, "farmcore.dump": b"PGDMP" + b"\x00" * 64},
    "dump replaced, manifest to match": lambda f: _replace_dump(f, b"PGDMP" + b"\x00" * 64),
    "manifest too large": lambda f: {**f, "manifest.json": b" " * (1 << 20) + f["manifest.json"]},
}


def _replace_dump(files: dict[str, bytes], dump: bytes) -> dict[str, bytes]:
    meta = {"name": "farmcore.dump", "bytes": len(dump), "sha256": hashlib.sha256(dump).hexdigest()}
    files = edit_manifest(dump=meta)(files)
    return {**files, "farmcore.dump": dump}


class Untouched:
    """Evidence that a refused restore changed nothing: the target database (same OID, no
    settings, no tables), the source installation, every bundle, and no plaintext left."""

    def __init__(self, dbs: Databases, source: str, target: str, bundles: list[Path], tmp: Path):
        self.dbs, self.source, self.target = dbs, source, target
        self.bundles, self.tmp = bundles, tmp
        self.before = self._state()

    def _state(self) -> tuple:
        target = self.dbs.query(
            self.target,
            "SELECT d.oid, (SELECT count(*) FROM pg_db_role_setting WHERE setdatabase = d.oid), "
            "(SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'), "
            # No staging database or loader role is left behind.
            "(SELECT count(*) FROM pg_database WHERE datname LIKE current_database() || '%'), "
            "(SELECT count(*) FROM pg_roles WHERE rolname LIKE 'farmcore_restore%') "
            "FROM pg_database d WHERE datname = current_database()",
        )
        bundles = [
            os.access(b, os.R_OK) and hashlib.sha256(b.read_bytes()).hexdigest()
            for b in self.bundles
        ]
        return target, table_rows(self.dbs, self.source), bundles

    def check(self) -> None:
        assert self._state() == self.before
        assert list(self.tmp.iterdir()) == []


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """The temporary directory backup and restore use (the filesystem boundary)."""
    import tempfile

    path = tmp_path / "tmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@pytest.fixture
def refusal(source, dbs, cli, owner_key, tmp_path, private_tmp):
    """A good bundle of A, an older one beside it, and an empty target. `run(bundle, key)`
    restores, expects a refusal and checks that nothing changed."""
    key, recipient = owner_key
    older = backup(cli, source, recipient, tmp_path / "older.farmcore-backup")
    good = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)

    def run(bundle: Path, identity: Path = key) -> Result:
        untouched = Untouched(dbs, source["name"], target, [older, good, bundle], private_tmp)
        result = cli("restore", str(bundle), "--identity", str(identity), env=env)
        assert result.exit_code == 1, result.output
        untouched.check()
        return result

    return run, good, key, recipient, tmp_path


def test_restore_refuses_a_wrong_key_before_touching_anything(refusal):
    run, good, _, _, tmp_path = refusal
    other = tmp_path / "keys" / "other.key"
    subprocess.run([AGE_KEYGEN, "-o", other], check=True, capture_output=True)

    assert "cannot decrypt" in run(good, other).output


@pytest.mark.parametrize("where", ["header", "middle", "last byte"])
def test_restore_refuses_a_modified_bundle(refusal, where):
    run, good, _, _, tmp_path = refusal
    data = bytearray(good.read_bytes())
    data[{"header": 40, "middle": len(data) // 2, "last byte": len(data) - 1}[where]] ^= 0x01
    modified = tmp_path / "modified.farmcore-backup"
    modified.write_bytes(bytes(data))

    assert "cannot decrypt" in run(modified).output


@pytest.mark.parametrize("fraction", [0.0, 0.01, 0.5, 0.999])
def test_restore_refuses_a_truncated_bundle(refusal, fraction):
    run, good, _, _, tmp_path = refusal
    data = good.read_bytes()
    truncated = tmp_path / "truncated.farmcore-backup"
    truncated.write_bytes(data[: min(int(len(data) * fraction), len(data) - 1)])

    assert "cannot decrypt" in run(truncated).output


def test_restore_refuses_a_missing_bundle(refusal):
    run, _, _, _, tmp_path = refusal

    assert "no bundle" in run(tmp_path / "nothing-here").output


@pytest.mark.parametrize("change", list(TAMPERED), ids=list(TAMPERED))
def test_restore_refuses_a_re_encrypted_bundle_whose_manifest_or_contents_were_changed(
    refusal, change
):
    run, good, key, recipient, tmp_path = refusal
    files = TAMPERED[change](members(decrypt(good, key)))
    forged = encrypt(repack(files), recipient, tmp_path / "forged.farmcore-backup")

    result = run(forged)

    assert result.output.startswith("restore: ") and "pg_restore failed" not in result.output


def test_restore_refuses_a_bundle_that_is_no_archive(refusal):
    run, _, _, recipient, tmp_path = refusal
    forged = encrypt(b"just some text\n" * 100, recipient, tmp_path / "forged.farmcore-backup")

    assert "not a FarmCore backup" in run(forged).output


def test_restore_refuses_a_bundle_from_an_incompatible_release(refusal, source, dbs, cli):
    run, _, _, recipient, tmp_path = refusal
    # A newer release's database: a schema revision this release has never heard of.
    dbs.query(
        source["name"], "UPDATE alembic_version SET version_num = 'zz_future_0001' RETURNING 1"
    )
    newer = backup(cli, source, recipient, tmp_path / "newer.farmcore-backup")

    assert "incompatible release" in run(newer).output


def test_restore_never_replaces_an_installation_that_holds_data(
    source, dbs, cli, owner_key, tmp_path
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    before = table_rows(dbs, source["name"])
    a_id = installation_id(dbs, source["name"])

    result = cli("restore", str(bundle), "--identity", str(key), env=source)

    assert result.exit_code == 1
    assert "already holds data" in result.output
    assert table_rows(dbs, source["name"]) == before
    assert installation_id(dbs, source["name"]) == a_id


def fake_tool(directory: Path, name: str, script: str) -> None:
    """A stand-in for an external binary (the process boundary), first on PATH."""
    directory.mkdir(exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)


def test_a_dump_whose_entry_list_cannot_be_read_is_reported_and_nothing_is_restored(
    source, dbs, cli, owner_key, tmp_path, monkeypatch, private_tmp
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    real = shutil.which("pg_restore")
    fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--list*) echo "pg_restore: error: could not read the entry list" >&2; exit 1;;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert "the dump is unreadable: pg_restore: error: could not read" in result.output
    untouched.check()


def test_a_schema_that_cannot_be_dumped_for_comparison_is_reported_and_nothing_is_restored(
    source, dbs, cli, owner_key, tmp_path, monkeypatch, private_tmp
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    fake_tool(tmp_path / "bin", "pg_dump", 'echo "pg_dump: error: connection lost" >&2; exit 1\n')
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert "pg_dump of the new schema failed: pg_dump: error: connection lost" in result.output
    untouched.check()


def test_a_failed_load_leaves_the_target_as_it_was_and_a_rerun_restores(
    source, dbs, cli, owner_key, tmp_path, monkeypatch, private_tmp
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    real = shutil.which("pg_restore")
    fake = tmp_path / "bin"
    # pg_restore dies once it writes into the target (say the disk filled up).
    fake_tool(
        fake,
        "pg_restore",
        f"""case "$*" in
  *--single-transaction*) echo "pg_restore: error: could not write" >&2; exit 1;;
  *) exec "{real}" "$@";;
esac
""",
    )
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)

    failed = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert failed.exit_code == 1
    assert "pg_restore failed: pg_restore: error: could not write" in failed.output
    # The staging copy and its loader role are gone; the target is as it was, and closed.
    untouched.check()
    shut_out = create_engine(dbs.runtime_url(env))
    with (
        pytest.raises(Exception, match="permission denied|does not exist|authentication"),
        shut_out.connect(),
    ):
        pass
    shut_out.dispose()

    monkeypatch.setenv("PATH", os.environ["PATH"].split(os.pathsep, 1)[1])
    again = cli("restore", str(bundle), "--identity", str(key), env=env)
    assert again.exit_code == 0, again.output
    assert dbs.query(target, SEMANTIC["records"]) == dbs.query(source["name"], SEMANTIC["records"])


FARMCORE = Path(sys.executable).with_name("farmcore")


class HeldLoad:
    """A real `farmcore restore` process whose pg_restore stands still when it would load rows,
    until released: the moment a signal, a second restore or another writer can hit it."""

    def __init__(self, tmp_path: Path, bundle: Path, key: Path, env: dict[str, str], tmp: Path):
        self.gate = tmp_path / "gate"
        self.gate.mkdir()
        real = shutil.which("pg_restore")
        fake_tool(tmp_path / "held-bin", "pg_restore", f"""case "$*" in
  *--single-transaction*)
    touch "{self.gate}/loading"
    i=0
    while [ ! -e "{self.gate}/release" ] && [ $i -lt 1200 ]; do sleep 0.1; i=$((i+1)); done
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
        full = {
            **os.environ,
            **env,
            "PATH": f"{tmp_path / 'held-bin'}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp),
        }
        self.proc = subprocess.Popen(
            [FARMCORE, "restore", str(bundle), "--identity", str(key)],
            env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        deadline = time.monotonic() + 180
        while not (self.gate / "loading").exists():
            assert self.proc.poll() is None, self.proc.communicate()[0]
            assert time.monotonic() < deadline, "restore never reached the load"
            time.sleep(0.1)

    def release(self) -> None:
        (self.gate / "release").touch()

    def result(self) -> tuple[int, str]:
        out, _ = self.proc.communicate(timeout=180)
        return self.proc.returncode, out


def staging_of(dbs: Databases, target: str) -> list[str]:
    """The staging databases of `target` (`<its first 32 characters>_restore_...` or
    `..._old_...`; targets sharing those 32 characters share this list)."""
    return [
        d for (d,) in dbs.query("postgres", "SELECT datname FROM pg_database ORDER BY 1")
        if re.match(re.escape(target[:32]) + r"_(restore|old)_", d)
    ]  # fmt: skip


def held_copy(dbs: Databases, target: str) -> tuple[str, str]:
    """The one staging database of `target` and its loader role, both present."""
    (staging,) = staging_of(dbs, target)
    role = f"farmcore_restore_{staging.rsplit('_', 1)[1]}"
    assert role_exists(dbs, role)
    return staging, role


def role_exists(dbs: Databases, role: str) -> bool:
    return dbs.query("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = :r", r=role) == [
        (1,)
    ]


@pytest.fixture
def caught_signals() -> Iterator[list[int]]:
    """Stop signals this test process receives land here, never on pytest's default handler
    (which would end the whole run): a restore that fails to handle one fails the test."""
    caught: list[int] = []
    previous = {s: signal.signal(s, lambda n, _f: caught.append(n)) for s in STOP_SIGNALS}
    try:
        yield caught
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def as_superuser(dbs: Databases) -> str:
    """Shell assignments for a fake tool's psql to act as the server's superuser."""
    return f"PGUSER={dbs.server.superuser} PGPASSWORD={dbs.server.password}"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_a_stopped_restore_removes_its_staging_copy_and_loader_role(
    source, dbs, cli, owner_key, tmp_path, private_tmp, monkeypatch, caught_signals, signum
):
    """`docker compose stop`, a closed terminal: the staging copy holds every row in clear.
    pg_restore notes what exists, then the restore process gets the signal mid-load."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    seen = tmp_path / "seen.txt"
    real, psql = shutil.which("pg_restore"), shutil.which("psql")
    listing = "SELECT datname FROM pg_database UNION ALL SELECT rolname FROM pg_roles"
    fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--single-transaction*)
    {as_superuser(dbs)} "{psql}" -X -At -d postgres -c "{listing}" > "{seen}"
    kill -{signum.name.removeprefix("SIG")} $PPID
    exec sleep 60;;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)
    handler = signal.getsignal(signum)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert f"restore: stopped by {signum.name}" in result.output, result.output
    assert "nothing was restored" in result.output
    existed = seen.read_text().split()
    staging = [n for n in existed if n.startswith(f"{target}_restore_")]
    role = f"farmcore_restore_{staging[0].rsplit('_', 1)[1]}"
    assert len(staging) == 1 and role in existed  # both were there at the signal
    assert staging_of(dbs, target) == [] and not role_exists(dbs, role)
    untouched.check()
    assert signal.getsignal(signum) == handler  # the caller's handler is back
    assert caught_signals == []  # restore took the signal itself


def stop_while_waiting(dbs: Databases, statement: str, release) -> threading.Thread:
    """A thread that sends this process SIGTERM once the server shows `statement` (a prefix)
    waiting, then calls `release` so the statement can finish after the signal."""

    def run() -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            waiting = dbs.query(
                "postgres",
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' "
                "AND query LIKE :p AND pid <> pg_backend_pid()",
                p=statement + "%",
            )
            if waiting != [(0,)]:
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        release()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_signal_while_the_staging_copy_is_created_still_removes_it(
    source, dbs, cli, owner_key, tmp_path, private_tmp, caught_signals
):
    """SIGTERM while `CREATE DATABASE` runs: the server finishes the create whatever the client
    does, so restore must wait for it and then drop the copy, not report a copy it left."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)
    # A session on the target (the template) makes CREATE DATABASE wait for it to leave.
    holder_engine = create_engine(dbs.server.url(target))
    holder = holder_engine.connect()
    holder.execute(text("SELECT 1"))
    stopper = stop_while_waiting(dbs, "CREATE DATABASE", holder.invalidate)

    try:
        result = cli("restore", str(bundle), "--identity", str(key), env=env)
    finally:
        stopper.join()
        holder_engine.dispose()

    assert result.exit_code == 1
    assert "restore: stopped by SIGTERM; nothing was restored" in result.output, result.output
    assert staging_of(dbs, target) == []
    untouched.check()
    assert caught_signals == []


def test_a_signal_while_the_loader_role_is_created_still_removes_it(
    source, dbs, cli, owner_key, tmp_path, private_tmp, caught_signals
):
    """SIGTERM while the loader role is being created: restore lets the creation finish, then
    drops the role and the copy."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)
    # A lock on the role catalog makes CREATE ROLE wait until it is released.
    locker_engine = create_engine(dbs.server.url("postgres"))
    locker = locker_engine.connect()
    locker.begin()
    locker.exec_driver_sql("LOCK TABLE pg_catalog.pg_authid IN EXCLUSIVE MODE")

    def release() -> None:
        locker.rollback()
        locker.close()

    stopper = stop_while_waiting(dbs, "CREATE ROLE", release)
    try:
        result = cli("restore", str(bundle), "--identity", str(key), env=env)
    finally:
        stopper.join()
        locker_engine.dispose()

    assert result.exit_code == 1
    assert "restore: stopped by SIGTERM; nothing was restored" in result.output, result.output
    assert staging_of(dbs, target) == []
    untouched.check()
    assert caught_signals == []


def test_a_signal_during_the_swap_waits_for_it_and_says_the_copy_is_in(
    source, member, dbs, cli, owner_key, tmp_path, monkeypatch, caught_signals
):
    """Stopped between the two renames, restore must neither drop the copy that now holds the
    rows nor say nothing was restored: the signal waits until the swap is done."""
    from farmcore.core.commands import restore

    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    swap = restore.Copy.swap

    def stopped_during(copy) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # `docker compose stop` at this very moment
        swap(copy)

    monkeypatch.setattr(restore.Copy, "swap", stopped_during)
    # Another thread in the process (libraries start them), which may receive the signal.
    idle = threading.Event()
    helper = threading.Thread(target=idle.wait, daemon=True)
    helper.start()
    try:
        result = cli("restore", str(bundle), "--identity", str(key), env=env)
    finally:
        idle.set()
        helper.join()

    assert result.exit_code == 1
    assert "stopped by SIGTERM after the copy was swapped in" in result.output, result.output
    assert "nothing was restored" not in result.output
    assert dbs.query(target, SEMANTIC["records"]) == dbs.query(source["name"], SEMANTIC["records"])
    assert dbs.query(target, "SELECT count(*) FROM local_session") == [(0,)]
    assert cli("effects", env=env).output.strip() == "external effects: paused"
    assert staging_of(dbs, target) == []
    assert caught_signals == []
    # `migrate` finishes the job, as the message says.
    assert cli("migrate", env=env).exit_code == 0


def stop_inside_a_weakref_callback(monkeypatch) -> list[str]:
    """SIGTERM handled while Python runs a weakref callback, as it may at any moment: Python
    drops whatever the handler raises there ("Exception ignored in ..."). The dropped
    exceptions' names land in the returned list."""
    dropped: list[str] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: dropped.append(type(u.exc_value).__name__))
    handler = signal.getsignal(signal.SIGTERM)

    class Collected:
        pass

    gone = Collected()
    ref = weakref.ref(gone, lambda _ref: handler(signal.SIGTERM, None))
    del gone  # the callback runs here
    assert ref() is None
    return dropped


@pytest.mark.parametrize("step", ["decrypt", "drop_loader"])
def test_a_stop_the_handler_could_not_raise_still_stops_the_restore_before_the_swap(
    source, dbs, cli, owner_key, tmp_path, private_tmp, monkeypatch, caught_signals, step
):
    """The handler ignores further stop signals once it has run, so a stop it raised into a
    callback, where it is lost, must still stop the restore: at the next step, and at the
    latest before the swap (after the last step, `drop_loader`)."""
    from farmcore.core.commands import restore

    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)
    dropped: list[str] = []
    owner = restore if step == "decrypt" else restore.Copy
    real = getattr(owner, step)

    def then_stopped(*args) -> None:
        real(*args)
        dropped.extend(stop_inside_a_weakref_callback(monkeypatch))

    monkeypatch.setattr(owner, step, then_stopped)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert dropped == ["Interrupted"]  # the handler's raise was lost
    assert result.exit_code == 1
    assert "restore: stopped by SIGTERM; nothing was restored" in result.output, result.output
    assert staging_of(dbs, target) == []
    untouched.check()
    assert caught_signals == []


@pytest.mark.parametrize("failure", ["refusal", "database"])
def test_a_setup_failure_after_the_swap_says_the_data_is_in_and_to_run_migrate(
    source, dbs, cli, owner_key, tmp_path, monkeypatch, failure
):
    """The runtime role cannot be set up (say, a concurrent change to the catalog): the rows
    are already in, so the message must not read as a failed restore."""
    from sqlalchemy.exc import OperationalError

    from farmcore.core.commands import migrate

    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    errors = {
        "refusal": migrate.MigrateError(
            f"could not set up the runtime role {env['RUNTIME_DATABASE_ROLE']}: "
            "tuple concurrently updated"
        ),
        "database": OperationalError("GRANT", {}, Exception("tuple concurrently updated")),
    }

    def fails(*_args) -> None:
        raise errors[failure]

    monkeypatch.setattr(migrate, "ensure_runtime_role", fails)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert "tuple concurrently updated" in result.output, result.output
    assert "the data is in, paused; run migrate to finish" in result.output, result.output
    assert dbs.query(target, SEMANTIC["records"]) == dbs.query(source["name"], SEMANTIC["records"])
    monkeypatch.undo()
    assert cli("migrate", env=env).exit_code == 0


def test_restores_and_a_migrate_started_together_all_finish(
    source, dbs, cli, owner_key, tmp_path, private_tmp
):
    """Each one revokes PUBLIC's access on every database of the server, and creates, renames
    and drops databases: run at the same moment, none may fail on another's catalog change."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    common = f"br_{uuid.uuid4().hex[:10]}_" + "x" * 30  # 44 characters
    targets = [dbs.create(name=f"{common}_{n}") for n in ("one", "two", "three")]
    envs = [dbs.installation(t) for t in targets]
    # Every restore waits at its last check of the bundle, then all go at once.
    gate = tmp_path / "gate"
    gate.mkdir()
    real = shutil.which("pg_restore")
    fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--table=alembic_version*)
    touch "{gate}/ready.$$"
    i=0
    while [ ! -e "{gate}/go" ] && [ $i -lt 1800 ]; do sleep 0.05; i=$((i+1)); done
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    path = f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"

    def start(*args: str, env: dict[str, str]) -> subprocess.Popen:
        full = {**os.environ, **env, "PATH": path, "TMPDIR": str(private_tmp)}
        return subprocess.Popen(
            [FARMCORE, *args], env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )  # fmt: skip

    restores = [start("restore", str(bundle), "--identity", str(key), env=e) for e in envs]
    deadline = time.monotonic() + 180
    while len(list(gate.glob("ready.*"))) < len(restores):
        assert time.monotonic() < deadline, "the restores never reached the gate"
        time.sleep(0.05)
    (gate / "go").touch()
    migrating = start("migrate", env={k: v for k, v in source.items() if k != "name"})
    done = [p.communicate(timeout=300)[0] for p in [*restores, migrating]]

    assert [p.returncode for p in [*restores, migrating]] == [0] * 4, done
    records = dbs.query(source["name"], SEMANTIC["records"])
    assert [dbs.query(t, SEMANTIC["records"]) for t in targets] == [records] * 3
    assert staging_of(dbs, targets[0]) == []


def test_migrates_of_running_installations_during_restores_never_deadlock(
    source, dbs, cli, owner_key, tmp_path, private_tmp
):
    """A migrate of an installation that already has its runtime role first revokes that
    role's CONNECT, a change to `pg_database`, then waits for the server-wide catalog lock:
    unless the lock comes first, two of them (or one and a restore) wait on each other and
    one dies with "deadlock detected". Real processes, repeated: the race is a timing one."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    other = dbs.create()
    running = [{k: v for k, v in source.items() if k != "name"}, dbs.installation(other)]
    assert cli("migrate", env=running[1]).exit_code == 0

    def start(*args: str, env: dict[str, str]) -> subprocess.Popen:
        full = {**os.environ, **env, "TMPDIR": str(private_tmp)}
        return subprocess.Popen(
            [FARMCORE, *args], env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )  # fmt: skip

    rounds, failed = [], []
    for _ in range(3):  # before the fix, every one of these deadlocked at least once
        restoring = start(
            "restore", str(bundle), "--identity", str(key), env=dbs.installation(dbs.create())
        )
        pairs = 0
        while restoring.poll() is None:  # two migrates at a time, for as long as it runs
            migrating = [start("migrate", env=e) for e in running]
            for p in migrating:
                out = p.communicate(timeout=300)[0]
                if p.returncode:
                    failed.append(out)
            pairs += 1
        out = restoring.communicate(timeout=300)[0]
        if restoring.returncode:
            failed.append(out)
        rounds.append(pairs)

    assert failed == [], failed
    assert min(rounds) >= 2, rounds  # the migrates did overlap each restore


@pytest.fixture
def short_lock_timeout(monkeypatch) -> str:
    from farmcore.core.commands import migrate, restore

    monkeypatch.setattr(migrate, "LOCK_TIMEOUT", "1s")
    monkeypatch.setattr(restore, "LOCK_TIMEOUT", "1s")
    return "1s"


@pytest.fixture
def database_list_locker(dbs) -> Iterator:
    """`lock()` holds `pg_database` locked, as a long restore or migrate step would, until the
    test ends or `unlock()`."""
    from farmcore.core.commands.migrate import LOCK_DATABASES

    engine = create_engine(dbs.server.url("postgres"))
    conn = engine.connect()

    class Locker:
        def lock(self) -> None:
            conn.begin()
            conn.exec_driver_sql(LOCK_DATABASES)

        def unlock(self) -> None:
            conn.rollback()

    try:
        yield Locker()
    finally:
        conn.close()
        engine.dispose()


def test_a_restore_kept_from_creating_its_copy_says_nothing_was_restored(
    source, dbs, cli, owner_key, tmp_path, private_tmp, short_lock_timeout, database_list_locker
):
    """CREATE DATABASE waits for `pg_database`, and cannot be stopped while it does (a signal
    waits for it): it gives up after the lock timeout, well within `docker compose stop`'s 10
    seconds, so a stop never has to end in SIGKILL and a left staging copy."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)
    database_list_locker.lock()
    started = time.monotonic()

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    waited = time.monotonic() - started
    database_list_locker.unlock()
    assert result.exit_code == 1
    assert "restore: the server is busy: another restore or migrate kept its list of " in (
        result.output
    )
    assert "locked for over 1s; nothing was restored, try again" in result.output, result.output
    assert waited < 10
    assert staging_of(dbs, target) == []
    untouched.check()
    again = cli("restore", str(bundle), "--identity", str(key), env=env)
    assert again.exit_code == 0, again.output


def test_a_restore_kept_from_the_database_list_after_the_swap_says_the_data_is_in(
    source, dbs, cli, owner_key, tmp_path, monkeypatch, short_lock_timeout, database_list_locker
):
    from farmcore.core.commands import restore

    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    swap = restore.Copy.swap

    def then_busy(copy) -> None:
        swap(copy)
        database_list_locker.lock()

    monkeypatch.setattr(restore.Copy, "swap", then_busy)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    database_list_locker.unlock()
    assert result.exit_code == 1
    assert "the server is busy" in result.output, result.output
    assert "after the copy was swapped in: the data is in, paused; run migrate to finish" in (
        result.output
    )
    assert "nothing was restored" not in result.output
    assert dbs.query(target, SEMANTIC["records"]) == dbs.query(source["name"], SEMANTIC["records"])
    assert cli("migrate", env=env).exit_code == 0


def test_restores_into_targets_with_a_common_prefix_never_meet(
    source, dbs, cli, owner_key, tmp_path, private_tmp
):
    """Two targets whose names share their first 40 characters: neither restore blocks the
    other or sweeps away the other's staging copy."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    common = f"br_{uuid.uuid4().hex[:10]}_" + "x" * 30
    first = dbs.create(name=common + "_one")
    second = dbs.create(name=common + "_two")
    first_env, second_env = dbs.installation(first), dbs.installation(second)
    held = HeldLoad(tmp_path, bundle, key, first_env, private_tmp)
    staging, role = held_copy(dbs, first)
    try:
        other = cli("restore", str(bundle), "--identity", str(key), env=second_env)
        still_there = staging in staging_of(dbs, first) and role_exists(dbs, role)
    finally:
        held.release()
    code, out = held.result()

    assert other.exit_code == 0, other.output
    assert still_there
    assert code == 0, out
    records = dbs.query(source["name"], SEMANTIC["records"])
    assert [dbs.query(n, SEMANTIC["records"]) for n in (first, second)] == [records, records]


def test_the_sweep_drops_only_staging_copies_its_owner_made(
    source, dbs, cli, owner_key, tmp_path, private_tmp
):
    """A database that looks like a stale staging copy but belongs to another role stays."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    held = HeldLoad(tmp_path, bundle, key, env, private_tmp)
    held.proc.kill()
    held.result()
    staging, _ = held_copy(dbs, target)
    stranger = f"rt_{uuid.uuid4().hex[:10]}"
    dbs.roles.append(stranger)
    dbs.names.append(staging)
    with dbs.admin.connect() as conn:
        conn.execute(text(f'CREATE ROLE "{stranger}" NOLOGIN'))
        conn.execute(text(f'ALTER DATABASE "{staging}" OWNER TO "{stranger}"'))

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 0, result.output
    assert staging_of(dbs, target) == [staging]


def test_the_next_restore_removes_plaintext_a_killed_restore_left_in_the_temp_dir(
    dbs, cli, owner_key, private_tmp
):
    """SIGKILL skips the temporary directory's cleanup; the next restore removes it once it is
    older than any restore runs. A recent one (a restore running now) and others stay."""
    key, _ = owner_key
    old = time.time() - 7 * 3600
    stale = private_tmp / "farmcore-restore-killed"
    stale.mkdir()
    (stale / "farmcore.dump").write_bytes(b"PGDMP plaintext rows")
    (private_tmp / "farmcore-restore-running").mkdir()
    (private_tmp / "unrelated").mkdir()
    for path in (stale / "farmcore.dump", stale, private_tmp / "unrelated"):
        os.utime(path, (old, old))
    env = dbs.installation(dbs.create())

    result = cli("restore", str(private_tmp / "nothing-here"), "--identity", str(key), env=env)

    assert result.exit_code == 1 and "no bundle" in result.output
    left = sorted(p.name for p in private_tmp.iterdir())
    assert left == ["farmcore-restore-running", "unrelated"]


def test_the_temp_dir_sweep_never_follows_a_link(
    dbs, cli, owner_key, tmp_path, private_tmp, monkeypatch
):
    """A stale-looking `farmcore-restore-*` link to a directory is not a restore's directory:
    the sweep leaves it, and never hands it to rmtree."""
    key, _ = owner_key
    old = time.time() - 7 * 3600
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "kept").write_text("not a restore's")
    link = private_tmp / "farmcore-restore-link"
    link.symlink_to(elsewhere, target_is_directory=True)
    os.utime(elsewhere, (old, old))
    os.utime(link, (old, old), follow_symlinks=False)
    removed: list[Path] = []
    monkeypatch.setattr(shutil, "rmtree", lambda path, **_kw: removed.append(Path(path)))
    env = dbs.installation(dbs.create())

    result = cli("restore", str(private_tmp / "nothing-here"), "--identity", str(key), env=env)

    assert result.exit_code == 1 and "no bundle" in result.output, result.output
    assert removed == []
    assert link.is_symlink() and (elsewhere / "kept").exists()


def test_the_temp_dir_sweep_skips_an_entry_that_vanishes_under_it(
    dbs, cli, owner_key, private_tmp, monkeypatch
):
    """Another restore removes its own directory between the listing and the look at it."""
    key, _ = owner_key
    old = time.time() - 7 * 3600
    stale = private_tmp / "farmcore-restore-killed"
    stale.mkdir()
    os.utime(stale, (old, old))
    listed = Path.glob

    def glob_then_vanish(self, pattern):
        yield self / "farmcore-restore-finished"  # listed, then removed by its own restore
        yield from listed(self, pattern)

    monkeypatch.setattr(Path, "glob", glob_then_vanish)
    env = dbs.installation(dbs.create())

    result = cli("restore", str(private_tmp / "nothing-here"), "--identity", str(key), env=env)

    assert result.exit_code == 1 and "no bundle" in result.output, result.output
    assert list(private_tmp.iterdir()) == []


def test_the_next_restore_removes_what_a_killed_restore_left_behind(
    source, dbs, cli, owner_key, tmp_path, private_tmp
):
    """SIGKILL (or a power cut) skips every cleanup; the next restore into the target does it."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    held = HeldLoad(tmp_path, bundle, key, env, private_tmp)
    held.proc.kill()
    held.result()
    _, role = held_copy(dbs, target)  # both left behind
    for d in os.listdir(private_tmp):  # a killed process leaves its own temp dir too
        shutil.rmtree(private_tmp / d)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 0, result.output
    assert staging_of(dbs, target) == [] and not role_exists(dbs, role)
    assert dbs.query(target, SEMANTIC["records"]) == dbs.query(source["name"], SEMANTIC["records"])


def test_a_second_restore_into_the_same_target_is_refused_and_the_target_is_rechecked(
    source, dbs, cli, owner_key, tmp_path, private_tmp
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    held = HeldLoad(tmp_path, bundle, key, env, private_tmp)
    _, role = held_copy(dbs, target)
    try:
        second = cli("restore", str(bundle), "--identity", str(key), env=env)
        # Meanwhile someone starts using the target (say its app came up and wrote a table).
        writer = create_engine(dbs.server.url(target))
        with writer.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE public.meanwhile (id int)")
        writer.dispose()
    finally:
        held.release()
    code, out = held.result()

    assert second.exit_code == 1
    assert f"another restore into {target} is running" in second.output
    assert code == 1 and "already holds data" in out, out
    assert dbs.query(target, "SELECT count(*) FROM public.meanwhile") == [(0,)]
    assert staging_of(dbs, target) == [] and not role_exists(dbs, role)


def test_a_swap_that_fails_halfway_puts_the_target_back(
    source, dbs, cli, owner_key, tmp_path, private_tmp, monkeypatch
):
    """The second rename fails (someone holds the staging copy open): the target keeps its
    name, and nothing is left."""
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    oid = dbs.query("postgres", "SELECT oid FROM pg_database WHERE datname = :d", d=target)
    real, psql = shutil.which("pg_restore"), shutil.which("psql")
    fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--single-transaction*)
    {as_superuser(dbs)} "{psql}" -X -q -c "SELECT pg_sleep(120)" >/dev/null 2>&1 &
    sleep 1
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert "restore: cannot use the database:" in result.output, result.output
    assert dbs.query("postgres", "SELECT oid FROM pg_database WHERE datname = :d", d=target) == oid
    assert staging_of(dbs, target) == []


def test_a_swap_that_fails_while_a_signal_waits_reports_the_swap_error(
    source, dbs, cli, owner_key, tmp_path, private_tmp, monkeypatch, caught_signals
):
    """SIGTERM arrives during a swap that then fails: the cause is the swap's error, not the
    signal, and the copy is still removed."""
    from sqlalchemy.exc import OperationalError

    from farmcore.core.commands import restore

    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    untouched = Untouched(dbs, source["name"], target, [bundle], private_tmp)

    def stopped_then_fails(_copy) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        raise OperationalError("ALTER DATABASE", {}, Exception("the rename failed"))

    monkeypatch.setattr(restore.Copy, "swap", stopped_then_fails)

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1
    assert result.output == "restore: cannot use the database: the rename failed\n"
    untouched.check()
    assert caught_signals == []


def test_a_bundle_from_an_older_schema_is_restored_and_upgraded_to_this_release(
    dbs, cli, owner_key, tmp_path
):
    """A bundle written before the identity tables existed: its rows load at its own revision,
    then the copy is upgraded to head."""
    key, recipient = owner_key
    old = dbs.create()
    env = dbs.installation(old)
    from alembic import command

    from farmcore.core.commands.migrate import alembic_config

    command.upgrade(alembic_config(env["DATABASE_URL"]), "inventory_0001")
    old_id = str(uuid.uuid4())
    setter = create_engine(dbs.server.url(old), isolation_level="AUTOCOMMIT")
    with setter.connect() as conn:  # what that release's `migrate` did
        conn.exec_driver_sql(f"ALTER DATABASE \"{old}\" SET farmcore.installation_id = '{old_id}'")
    setter.dispose()
    dbs.query(old, "INSERT INTO farm (name) VALUES ('Old farm') RETURNING id")
    dbs.query(
        old,
        "INSERT INTO log (farm_id, type, status, name, ts_start) "
        "SELECT id, 'spray', 'done', 'Copper', now() FROM farm RETURNING id",
    )
    bundle = backup(cli, {**env, "name": old}, recipient, tmp_path / "old.farmcore-backup")
    assert json.loads(members(decrypt(bundle, key))["manifest.json"])["alembic_heads"] == [
        "inventory_0001"
    ]
    target = dbs.create()
    target_env = dbs.installation(target)

    result = cli("restore", str(bundle), "--identity", str(key), env=target_env)

    assert result.exit_code == 0, result.output
    assert dbs.query(target, "SELECT version_num FROM alembic_version") == [(HEAD,)]
    joined = "SELECT f.name, l.name FROM log l JOIN farm f ON f.id = l.farm_id"
    assert dbs.query(target, joined) == [("Old farm", "Copper")]
    assert dbs.query(target, "SELECT count(*) FROM local_session") == [(0,)]


def test_pg_dump_reads_the_snapshot_the_manifest_was_read_in(
    source, dbs, cli, owner_key, tmp_path, monkeypatch
):
    """Between the manifest's read and pg_dump's start, another writer switches an app on: the
    dump still shows the moment the manifest describes."""
    key, recipient = owner_key
    real, psql = shutil.which("pg_dump"), shutil.which("psql")
    update = "UPDATE app_module SET state = 'installed' WHERE name = 'kpi'"
    fake_tool(tmp_path / "bin", "pg_dump", f"""case "$*" in
  *--version*) exec "{real}" "$@";;
  *) "{psql}" -X -q -v ON_ERROR_STOP=1 -c "{update}" >/dev/null || exit 1
     exec "{real}" "$@";;
esac
""")  # fmt: skip
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")

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


def test_a_restore_that_stops_after_the_data_never_carries_sessions_or_opens_the_copy(
    source, member, dbs, cli, owner_key, tmp_path
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    assert dbs.query(source["name"], "SELECT count(*) FROM local_session") == [(1,)]
    target = dbs.create()
    # The runtime role setup refuses (it names the owner), after the data is in.
    env = {**dbs.installation(target), "RUNTIME_DATABASE_ROLE": dbs.server.superuser}

    result = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert result.exit_code == 1 and "must differ from the owner" in result.output
    assert dbs.query(target, "SELECT count(*) FROM log") == [(10,)]
    assert dbs.query(target, "SELECT count(*) FROM local_session") == [(0,)]
    assert cli("effects", env=env).output.strip() == "external effects: paused"


def test_restore_runs_only_as_the_servers_superuser(source, dbs, cli, owner_key, tmp_path):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    target = dbs.create()
    env = dbs.installation(target)
    assert cli("migrate", env=env).exit_code == 0  # creates the runtime role
    with dbs.admin.connect() as conn:  # a new database the runtime role may open, empty
        other = dbs.create()
        conn.execute(
            text(f'GRANT CONNECT ON DATABASE "{other}" TO "{env["RUNTIME_DATABASE_ROLE"]}"')
        )
    runtime = dbs.server.url(other, env["RUNTIME_DATABASE_ROLE"], env["RUNTIME_DATABASE_PASSWORD"])

    result = cli("restore", str(bundle), "--identity", str(key), env={"DATABASE_URL": runtime})

    assert result.exit_code == 1
    assert "bootstrap superuser" in result.output


def test_backup_refuses_to_overwrite_or_to_write_without_a_recipient(
    source, cli, owner_key, tmp_path
):
    _, recipient = owner_key
    existing = tmp_path / "a.farmcore-backup"
    existing.write_bytes(b"an older bundle")

    clash = cli("backup", "--recipient", recipient, "--output", str(existing), env=source)
    nobody = cli("backup", "--output", str(tmp_path / "b.farmcore-backup"), env=source)

    assert clash.exit_code == 1 and "already exists" in clash.output
    assert existing.read_bytes() == b"an older bundle"
    assert nobody.exit_code == 1 and "recipient" in nobody.output
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["a.farmcore-backup"]


def test_backup_reads_the_recipient_from_the_environment(source, cli, owner_key, tmp_path):
    key, recipient = owner_key
    out = tmp_path / "a.farmcore-backup"

    result = cli("backup", "--output", str(out), env={**source, "BACKUP_RECIPIENT": recipient})

    assert result.exit_code == 0, result.output
    assert json.loads(members(decrypt(out, key))["manifest.json"])["format"] == "farmcore-backup/1"


@pytest.mark.parametrize(
    ("tool", "script", "message"),
    [
        ("pg_dump", 'echo "pg_dump: error: server version mismatch" >&2; exit 1\n',
         "pg_dump failed: pg_dump: error: server version mismatch"),
        ("age", 'cat > /dev/null; echo "age: error: disk full" >&2; exit 1\n',
         "age failed: age: error: disk full"),
    ],
)  # fmt: skip
def test_a_failed_backup_leaves_no_file_and_no_plaintext(
    source, cli, owner_key, tmp_path, private_tmp, monkeypatch, tool, script, message
):
    _, recipient = owner_key
    fake_tool(tmp_path / "bin", tool, script)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = cli("backup", "-r", recipient, "-o", str(out_dir / "a.farmcore-backup"), env=source)

    assert result.exit_code == 1
    assert message in result.output
    assert list(out_dir.iterdir()) == [] and list(private_tmp.iterdir()) == []


def test_backup_rejects_a_malformed_recipient_and_leaves_no_file(source, cli, tmp_path):
    out = tmp_path / "a.farmcore-backup"

    result = cli("backup", "-r", "age1notakey", "-o", str(out), env=source)

    assert result.exit_code == 1 and "age failed: age: error:" in result.output
    assert "filippo.io" not in result.output  # age's footer, not the cause
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("recipient", ["ssh-ed25519 AAAAC3Nza", "AGE-SECRET-KEY-1XYZ", "bob"])
def test_backup_takes_only_age_public_keys(source, cli, tmp_path, recipient):
    result = cli("backup", "-r", recipient, "-o", str(tmp_path / "a"), env=source)

    assert result.exit_code == 1 and "age public keys (age1...)" in result.output
    assert recipient not in result.output  # never echo what might be a private key
    assert list(tmp_path.iterdir()) == []


def test_backup_into_an_unwritable_directory_fails_in_one_line(source, cli, owner_key, tmp_path):
    _, recipient = owner_key
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        result = cli("backup", "-r", recipient, "-o", str(locked / "a"), env=source)
        missing = cli("backup", "-r", recipient, "-o", str(tmp_path / "no" / "a"), env=source)
    finally:
        locked.chmod(0o700)

    assert result.exit_code == 1 and result.output.count("\n") == 1
    assert "cannot write" in result.output
    assert missing.exit_code == 1 and "cannot write" in missing.output


def test_backup_refuses_a_server_newer_than_its_pg_dump(
    source, cli, owner_key, tmp_path, monkeypatch
):
    _, recipient = owner_key
    fake_tool(tmp_path / "bin", "pg_dump", 'echo "pg_dump (PostgreSQL) 16.4"; exit 0\n')
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    result = cli("backup", "-r", recipient, "-o", str(tmp_path / "a"), env=source)

    assert result.exit_code == 1
    assert "PostgreSQL 17" in result.output and "pg_dump 16" in result.output


def test_the_manifest_and_the_dump_describe_one_moment(source, dbs, cli, owner_key, tmp_path):
    """While an app is switched on and off, every bundle's manifest says what its dump holds."""
    import threading

    key, recipient = owner_key
    stop = threading.Event()

    def toggle() -> None:
        engine = create_engine(dbs.server.url(source["name"]))
        while not stop.is_set():
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE app_module SET state = CASE state WHEN 'installed' THEN "
                    "'uninstalled' ELSE 'installed' END WHERE name = 'kpi'"
                ))  # fmt: skip
        engine.dispose()

    writer = threading.Thread(target=toggle)
    writer.start()
    try:
        bundles = [backup(cli, source, recipient, tmp_path / f"{i}.age") for i in range(6)]
    finally:
        stop.set()
        writer.join()

    for bundle in bundles:
        files = members(decrypt(bundle, key))
        dump = tmp_path / "x.dump"
        dump.write_bytes(files["farmcore.dump"])
        sql = subprocess.run(
            ["pg_restore", "--data-only", "--table=app_module", "-f", "-", dump],
            check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip
        in_dump = re.search(r"^kpi\t(\w+)\t", sql, re.M).group(1)
        in_manifest = json.loads(files["manifest.json"])["modules"]["kpi"]["state"]
        assert in_dump == in_manifest


def test_backup_and_restore_say_which_tool_is_missing(source, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = cli("backup", "-r", "age1x", "-o", str(tmp_path / "a"), env=source)
    assert result.exit_code == 1 and "`age` is not installed" in result.output


def test_backup_and_restore_report_an_unreachable_database_without_its_password(
    source, cli, owner_key, tmp_path, pg_server
):
    key, recipient = owner_key
    bundle = backup(cli, source, recipient, tmp_path / "a.farmcore-backup")
    password = secrets.token_hex(12)
    env = {"DATABASE_URL": pg_server.url("no_such_database", "nobody", password)}

    backed = cli("backup", "-r", recipient, "-o", str(tmp_path / "b"), env=env)
    restored = cli("restore", str(bundle), "--identity", str(key), env=env)

    assert backed.exit_code == 1 and backed.output.startswith("backup: cannot read the database")
    assert restored.exit_code == 1
    assert restored.output.startswith("restore: cannot use the database")
    assert "authentication failed" in backed.output + restored.output
    assert password not in backed.output + restored.output


# --- Real Compose projects: back up A, restore into a new C, B keeps serving ----------------


def _psql(project, sql: str) -> list[str]:
    out = project.compose("exec", "-T", "db", "psql", "-U", "farmcore", "-d", "farmcore",
                          "-v", "ON_ERROR_STOP=1", "-Atc", sql).stdout  # fmt: skip
    return out.splitlines()


@pytest.mark.slow
def test_a_backup_of_project_a_restores_into_a_new_project_c_while_b_keeps_serving(
    new_project, owner_key, tmp_path
):
    import threading
    import time

    from tests.instances.conftest import inspect

    key, recipient = owner_key
    a, b = new_project(BACKUP_RECIPIENT=recipient), new_project()
    a.up()
    b.up()
    for args in (("seed", "--scene", "B"), ("replay", "--to", "2024-04-10 06:00")):
        a.compose("exec", "-T", "app", "farmcore", *args)
    a_id, b_id = a.health()["installation_id"], b.health()["installation_id"]

    # The compose `backup` service (its daily run at start, then this one-off) writes bundles
    # into A's `backups` volume; the latest holds the seeded farm.
    a.compose("run", "--rm", "--no-deps", "-e", "BACKUP_ONCE=1", "backup")
    listed = a.compose("run", "--rm", "--no-deps", "--entrypoint", "ls", "backup", "/backups")
    names = sorted(listed.stdout.split())
    assert len(names) == 2, names
    assert all(re.fullmatch(r"farmcore-\d{8}T\d{6}Z\.age", n) for n in names), names
    # Copied off the server as the docs say, then made private as a user would keep them.
    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    a.compose("cp", f"backup:/backups/{names[-1]}", str(restore_dir / "a.age"))
    bundle = restore_dir / "a.age"
    manifest = json.loads(members(decrypt(bundle, key))["manifest.json"])
    assert manifest["installation_id"] == a_id
    shutil.copy(key, restore_dir / "owner.key")
    for path in restore_dir.iterdir():
        path.chmod(0o600)

    # A killed backup's plaintext temp directory is removed when the loop starts again.
    a.compose("exec", "-T", "backup", "mkdir", "/tmp/farmcore-backup-stale")
    a.compose("restart", "backup")
    deadline = time.monotonic() + 60
    while "farmcore-backup-stale" in a.compose("exec", "-T", "backup", "ls", "/tmp").stdout:
        assert time.monotonic() < deadline, "stale backup temp dir never removed"
        time.sleep(1)
    # B has no BACKUP_RECIPIENT: its backup service keeps running and says why, hourly.
    b_backup = inspect(b.container("backup"))
    assert b_backup["State"]["Running"] and b_backup["RestartCount"] == 0
    b_logs = b.compose("logs", "backup")
    assert "FAILED: set BACKUP_RECIPIENT" in b_logs.stdout + b_logs.stderr

    # B is probed every half second while C is created and restored.
    failures: list[str] = []
    done = threading.Event()

    def probe() -> None:
        while not done.is_set():
            try:
                assert b.health() == {"db": "ok", "installation_id": b_id}
            except Exception as exc:  # noqa: BLE001 - any failure to serve counts
                failures.append(type(exc).__name__)
            time.sleep(0.5)

    watcher = threading.Thread(target=probe)
    watcher.start()
    try:
        c = new_project()
        c.compose("up", "-d", "--wait", "db")
        # Files the container's user (uid 10001) may not read: the Linux case. A root-owned
        # 0600 volume reproduces it on any host.
        volume = f"{c.name}_restore-probe"
        subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
        try:
            subprocess.run(
                ["docker", "run", "--rm", "--user", "0", "-v", f"{volume}:/r",
                 "-v", f"{restore_dir}:/src:ro", "--entrypoint", "sh", c.env["FARMCORE_IMAGE"],
                 "-c", "cp /src/a.age /src/owner.key /r/ && chmod 600 /r/*"],
                check=True, capture_output=True,
            )  # fmt: skip
            locked = c.compose(
                "run", "--rm", "--no-deps", "-v", f"{volume}:/restore:ro", "migrate",
                "farmcore", "restore", "/restore/a.age", "--identity", "/restore/owner.key",
                check=False,
            )  # fmt: skip
        finally:
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)
        assert locked.returncode == 1
        assert "cannot read the bundle" in locked.stderr and "--user" in locked.stderr
        # The documented command: run as the user who owns the files.
        restored = c.compose(
            "run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{restore_dir}:/restore:ro", "migrate",
            "farmcore", "restore", "/restore/a.age", "--identity", "/restore/owner.key",
            check=False,
        )  # fmt: skip
        assert restored.returncode == 0, restored.stdout + restored.stderr
        c.up()
    finally:
        done.set()
        watcher.join()
    assert failures == []

    c_id = c.health()["installation_id"]
    assert len({a_id, b_id, c_id}) == 3
    effects = c.compose("run", "--rm", "--no-deps", "migrate", "farmcore", "effects")
    assert effects.stdout.strip() == "external effects: paused"
    for label, sql in SEMANTIC.items():
        assert _psql(c, sql) == _psql(a, sql), label
    assert len(_psql(c, SEMANTIC["records"])) == 10
    assert _psql(c, "SELECT count(*) FROM local_session") == ["0"]
    assert b.health() == {"db": "ok", "installation_id": b_id}
    assert a.health() == {"db": "ok", "installation_id": a_id}
