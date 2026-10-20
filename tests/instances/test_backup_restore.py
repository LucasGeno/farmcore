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
import subprocess
import tarfile
import uuid
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

    def create(self, *, postgis: bool = True) -> str:
        name = f"br_{uuid.uuid4().hex[:10]}"
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
}


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
            "(SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public') "
            "FROM pg_database d WHERE datname = current_database()",
        )
        bundles = [b.exists() and hashlib.sha256(b.read_bytes()).hexdigest() for b in self.bundles]
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
