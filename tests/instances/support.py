"""Helpers shared by the installation tests: databases on the throwaway server, the `farmcore`
CLI, bundles as an attacker or an owner handles them, and the evidence a refused restore
changed nothing. Fixtures live in conftest.py."""

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
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner, Result

from farmcore.settings import get_settings

AGE = shutil.which("age")
AGE_KEYGEN = shutil.which("age-keygen")
HEAD = "review_0001"  # the single supported Alembic head of this release
# Next to this interpreter in the project venv; on PATH when the interpreter is an overlay
# (`uv run --with …` has no entry scripts of its own).
FARMCORE = Path(sys.executable).with_name("farmcore")
if not FARMCORE.exists() and shutil.which("farmcore"):
    FARMCORE = Path(shutil.which("farmcore"))

needs_age = pytest.mark.skipif(
    AGE is None or AGE_KEYGEN is None, reason="needs the age CLI (age, age-keygen)"
)

# What an operator reads in a refusal or a stop: one key phrase per outcome, never a sentence.
OUTCOMES = {
    "cannot decrypt": "cannot decrypt",
    "not a backup": "not a FarmCore backup",
    "no bundle": "no bundle",
    "unreadable bundle": "cannot read the bundle",
    "unreadable key": "cannot read the key file",
    "user hint": "--user",
    "older release": "Upgrading",
    "holds data": "already holds data",
    "incompatible": "incompatible",
    "busy": "server is busy",
    "nothing restored": "nothing was restored",
    "restored": ") restored",
    "revert image": "set the previous image again",
    "data is in": "the data is in",
    "run migrate": "run migrate",
    "farmcore migrate": "`farmcore migrate`",
    "superuser": "bootstrap superuser",
    "stopped": "stopped by SIGTERM",
    "nothing migrated": "nothing was migrated",
    "nothing done": "nothing was done",
    "another migrator": "another upgrade or migrate is running",
    "another restore": "another restore into",
    "load failed": "pg_restore failed",
    "foreign objects": "not part of FarmCore",
    "database unusable": "cannot use the database",
    "never migrated": "never migrated",
    "drop hint": "drop it with DROP DATABASE",
}


def says(output: str, *outcomes: str) -> None:
    for outcome in outcomes:
        assert OUTCOMES[outcome] in output, (outcome, output)


def refused(result: Result, *outcomes: str) -> str:
    """The command failed (exit 1) and says each outcome; its output."""
    assert result.exit_code == 1, result.output
    says(result.output, *outcomes)
    return result.output


def run_cli(*args: str, env: dict[str, str], input: str | None = None, obj=None) -> Result:
    """`farmcore ...` in this process with `env` set for the call, as an operator runs it."""
    from farmcore.cli import cli as app

    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        get_settings.cache_clear()
        try:
            return CliRunner().invoke(app, list(args), input=input, obj=obj, catch_exceptions=False)
        finally:
            get_settings.cache_clear()


def pg_bindir() -> str | None:
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


def make_key(directory: Path) -> tuple[Path, str]:
    """An age identity file in `directory` and its public recipient."""
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / f"{uuid.uuid4().hex[:6]}.key"
    subprocess.run([AGE_KEYGEN, "-o", key], check=True, capture_output=True)
    recipient = subprocess.run(
        [AGE_KEYGEN, "-y", key], check=True, capture_output=True, text=True
    ).stdout.strip()
    return key, recipient


class Databases:
    """New databases (PostGIS enabled) on the throwaway server, each with its own runtime role."""

    def __init__(self, server, templates: Templates | None = None) -> None:
        self.server = server
        self.templates = templates
        self.admin = create_engine(server.url("farmcore"), isolation_level="AUTOCOMMIT")
        self.names: list[str] = []
        self.roles: list[str] = []
        self.locked: list[str] = []

    def create(
        self, *, postgis: bool = True, name: str | None = None, template: str | None = None
    ) -> str:
        """A new database: a copy of `template`, else bare with PostGIS (also from a template
        when the session has one), else bare."""
        name = name or f"br_{uuid.uuid4().hex[:10]}"
        if template is None and postgis and self.templates is not None:
            template = self.templates.bare
        copy = f' TEMPLATE "{template}"' if template else ""
        with self.admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"{copy}'))
        self.names.append(name)
        if postgis and not template:
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
                result = conn.execute(text(sql), params)
                return [tuple(r) for r in result] if result.returns_rows else []
        finally:
            db.dispose()

    def on_server(self, statement: str) -> None:
        """A statement that runs outside a transaction (ALTER or DROP DATABASE)."""
        with self.admin.connect() as conn:
            conn.execute(text(statement))

    def lock_as_template(self, name: str, role: str) -> None:
        """Make `name` a template no clone inherits anything foreign from. A copy carries every
        grant inside the database, so the builder's runtime role (cluster-wide, it would outlive
        the copy) loses them and is dropped; settings and the database ACL are never copied."""
        self.query(name, f'DROP OWNED BY "{role}"')
        self.on_server(f'DROP OWNED BY "{role}"')
        self.on_server(f'DROP ROLE "{role}"')
        self.on_server(f'ALTER DATABASE "{name}" RESET ALL')
        # A connection left open would make a copy fail now and then; none may open again.
        self.on_server(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false')
        self.on_server(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}'"
        )
        self.locked.append(name)

    def close(self) -> None:
        """Drop every database and role made here, and what a restore into one of them left
        behind (a staging or emptied copy, and its loader role)."""
        with self.admin.connect() as conn:
            for name in self.locked:
                conn.execute(text(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE false'))
            every = [d for (d,) in conn.execute(text("SELECT datname FROM pg_database"))]
            for name in list(self.names):
                for d in every:
                    if left := re.match(re.escape(name[:32]) + r"_(restore|old)_(\w+)$", d):
                        self.names.append(d)
                        self.roles.append(f"farmcore_restore_{left[2]}")
            for name in dict.fromkeys(self.names):
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            for role in dict.fromkeys(self.roles):
                if conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                ).first():
                    conn.execute(text(f'DROP OWNED BY "{role}"'))
                    conn.execute(text(f'DROP ROLE "{role}"'))
        self.admin.dispose()


SEEDED = (
    ("migrate",),
    ("seed", "--scene", "B"),
    ("replay", "--to", "2024-04-10 06:00"),
    ("apps", "--uninstall", "kpi"),
)


def built_installation(
    dbs: Databases, steps=SEEDED, template: str | None = None, name: str | None = None
) -> dict:
    """An installation made by running `steps` through the CLI on a new database (or a copy of
    `template`). Installation A by default: the demo farm seeded and replayed, `kpi` off."""
    name = dbs.create(template=template, name=name)
    env = dbs.installation(name)
    for args in steps:
        result = run_cli(*args, env=env)
        assert result.exit_code == 0, result.output
    return {**env, "name": name}


class Templates:
    """Databases built once per session and only ever copied (`CREATE DATABASE … TEMPLATE`).
    A copy has no installation id, runtime role or database rights until its own `migrate`,
    which then only sets those up: the schema is already at head."""

    def __init__(self, dbs: Databases) -> None:
        self.bare = dbs.create(name=f"tpl_bare_{uuid.uuid4().hex[:6]}")
        dbs.on_server(f'ALTER DATABASE "{self.bare}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false')
        dbs.locked.append(self.bare)
        self.migrated = self._build(dbs, "migrated", (("migrate",),))
        self.seeded = self._build(dbs, "seeded", SEEDED)

    @staticmethod
    def _build(dbs: Databases, kind: str, steps) -> str:
        made = built_installation(dbs, steps, name=f"tpl_{kind}_{uuid.uuid4().hex[:6]}")
        dbs.lock_as_template(made["name"], made["RUNTIME_DATABASE_ROLE"])
        return made["name"]


def installation_from(dbs: Databases, template: str) -> dict[str, str]:
    """A copy of `template` migrated as a new installation with its own runtime role."""
    return built_installation(dbs, (("migrate",),), template=template)


def owner_env(installation: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in installation.items() if k != "name"}


def installation_id(dbs: Databases, name: str) -> str | None:
    rows = dbs.query(
        name,
        "SELECT substr(cfg, 26) FROM pg_db_role_setting s "
        "JOIN pg_database d ON d.oid = s.setdatabase, unnest(s.setconfig) cfg "
        "WHERE d.datname = current_database() AND cfg LIKE 'farmcore.installation_id=%'",
    )
    return rows[0][0] if rows else None


def backup(env: dict[str, str], recipient: str, path: Path) -> Path:
    result = run_cli("backup", "--recipient", recipient, "--output", str(path), env=owner_env(env))
    assert result.exit_code == 0, result.output
    return path


def restore(bundle: Path, key: Path, env: dict[str, str], on_step=None) -> Result:
    obj = {"on_step": on_step} if on_step else None
    return run_cli("restore", str(bundle), "--identity", str(key), env=env, obj=obj)


def effects(env: dict[str, str]) -> str:
    return run_cli("effects", env=env).output.strip()


def decrypt(bundle: Path, key: Path) -> bytes:
    return subprocess.run([AGE, "-d", "-i", key, bundle], check=True, capture_output=True).stdout


def members(plain: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


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


def with_manifest(files: dict[str, bytes], **changes) -> dict[str, bytes]:
    manifest = {**json.loads(files["manifest.json"]), **changes}
    return {**files, "manifest.json": json.dumps(manifest).encode()}


def replace_dump(files: dict[str, bytes], dump: bytes) -> dict[str, bytes]:
    """A new dump with a manifest to match (size and SHA-256), as a forger writes it."""
    meta = {"name": "farmcore.dump", "bytes": len(dump), "sha256": hashlib.sha256(dump).hexdigest()}
    return {**with_manifest(files, dump=meta), "farmcore.dump": dump}


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


def same_records(dbs: Databases, a: str, b: str) -> bool:
    return dbs.query(a, SEMANTIC["records"]) == dbs.query(b, SEMANTIC["records"])


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


def fake_tool(directory: Path, name: str, script: str) -> str:
    """A stand-in for an external binary (the process boundary); put `directory` first on PATH."""
    directory.mkdir(exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    return f"{directory}{os.pathsep}{os.environ['PATH']}"


def as_superuser(dbs: Databases) -> str:
    """Shell assignments for a fake tool's psql to act as the server's superuser."""
    return f"PGUSER={dbs.server.superuser} PGPASSWORD={dbs.server.password}"


class HeldLoad:
    """A real `farmcore restore` process whose pg_restore stands still when it would load rows,
    until released: the moment a signal, a second restore or another writer can hit it."""

    def __init__(self, tmp_path: Path, bundle: Path, key: Path, env: dict[str, str], tmp: Path):
        self.gate = tmp_path / f"gate-{uuid.uuid4().hex[:6]}"
        self.gate.mkdir()
        real = shutil.which("pg_restore")
        path = fake_tool(tmp_path / f"held-{self.gate.name}", "pg_restore", f"""case "$*" in
  *--single-transaction*)
    touch "{self.gate}/loading"
    i=0
    while [ ! -e "{self.gate}/release" ] && [ $i -lt 1200 ]; do sleep 0.05; i=$((i+1)); done
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
        full = {**os.environ, **env, "PATH": path, "TMPDIR": str(tmp)}
        self.proc = subprocess.Popen(
            [FARMCORE, "restore", str(bundle), "--identity", str(key)],
            env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        wait_for(lambda: (self.gate / "loading").exists() or self.proc.poll() is not None,
                 "the restore to reach the load", 180)  # fmt: skip
        assert self.proc.poll() is None, self.proc.communicate()[0]

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


def role_exists(dbs: Databases, role: str) -> bool:
    return dbs.query("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = :r", r=role) == [
        (1,)
    ]


def held_copy(dbs: Databases, target: str) -> tuple[str, str]:
    """The one staging database of `target` and its loader role, both present."""
    (staging,) = staging_of(dbs, target)
    role = f"farmcore_restore_{staging.rsplit('_', 1)[1]}"
    assert role_exists(dbs, role)
    # Teardown drops both even when a test keeps them, or fails before a restore sweeps them:
    # a loader role left on the session's server is swept by some later test's restore.
    dbs.names.append(staging)
    dbs.roles.append(role)
    return staging, role


def wait_for(predicate, what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def lock_waiters(dbs: Databases, prefix: str) -> int:
    """Client sessions waiting for a lock while running a statement that starts with `prefix`."""
    return dbs.query(
        "postgres",
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
        "AND backend_type = 'client backend' AND query LIKE :p",
        p=prefix + "%",
    )[0][0]


class DatabaseList:
    """`pg_database` held locked, as another restore's or migrate's step would hold it, from
    `lock()` until `unlock()`. A safety timer gives it back after `hold` seconds, so a
    regression that waits without a timeout fails late instead of hanging the run."""

    def __init__(self, dbs: Databases) -> None:
        from farmcore.core.commands.migrate import LOCK_DATABASES

        self.dbs = dbs
        self.statement = LOCK_DATABASES
        self.engine = create_engine(dbs.server.url("postgres"))
        self.conn = self.engine.connect()
        self.guard = threading.Lock()
        self.timer: threading.Timer | None = None

    def lock(self, hold: float = 10) -> None:
        self.conn.begin()
        self.conn.exec_driver_sql(self.statement)
        self.timer = threading.Timer(hold, self.unlock)
        self.timer.start()

    def unlock(self) -> None:
        with self.guard:
            if self.conn.in_transaction():
                self.conn.rollback()

    def unlock_once_waiting(self, prefix: str) -> threading.Thread:
        """Give the list back as soon as a statement starting with `prefix` waits for it."""

        def run() -> None:
            wait_for(lambda: lock_waiters(self.dbs, prefix) > 0, f"{prefix} to wait", 30)
            self.unlock()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        if self.timer:
            self.timer.cancel()
        self.unlock()
        self.conn.close()
        self.engine.dispose()


# --- Forged bundles: what an attacker with the owner's public key builds ----------------------


def forge_dump(dbs: Databases, files: dict[str, bytes], tmp: Path, sql: list[str], **opts) -> bytes:
    """What an attacker does on their own server: load A's dump, add `sql`, dump again."""
    scratch = dbs.create(postgis=False)
    original = tmp / f"orig-{scratch}.dump"
    original.write_bytes(files["farmcore.dump"])
    env = {
        **os.environ,
        "PGHOST": dbs.server.host,
        "PGPORT": str(dbs.server.port),
        "PGUSER": dbs.server.superuser,
        "PGPASSWORD": dbs.server.password,
    }
    subprocess.run(
        ["pg_restore", "--no-owner", "--no-acl", "-d", scratch, original],
        check=True, capture_output=True, env=env,
    )  # fmt: skip
    engine = create_engine(dbs.server.url(scratch))
    with engine.begin() as conn:
        for statement in sql:
            conn.exec_driver_sql(statement.replace("{db}", scratch))
    engine.dispose()
    out = tmp / f"forged-{scratch}.dump"
    args = ["pg_dump", "-Fc", "--no-acl", "-d", scratch, "-f", out]
    if opts.get("create"):
        args.insert(2, "--create")
    subprocess.run(args, check=True, capture_output=True, env=env)
    return out.read_bytes()


def forged_bundle(files: dict[str, bytes], dump: bytes, recipient: str, path: Path) -> Path:
    return encrypt(repack(replace_dump(files, dump)), recipient, path)


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

    def function(self) -> str:
        return (
            "CREATE FUNCTION public.fc_pwn() RETURNS int LANGUAGE plpgsql AS $f$ BEGIN "
            f"EXECUTE '{self.program()}'; RETURN 1; END $f$"
        )


# --- HTTP as a member ---------------------------------------------------------------------------

BASE = "https://testserver"


def app_for(database_url: str):
    from farmcore.main import create_app
    from farmcore.settings import Settings

    return create_app(Settings(_env_file=None, database_url=database_url))


def sign_in(database_url: str, login: str, password: str) -> dict[str, str]:
    """A member session on the installation at `database_url`: its cookies, as the browser
    holds them (names carry the installation)."""
    from fastapi.testclient import TestClient

    with TestClient(app_for(database_url), base_url=BASE) as c:
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

    with TestClient(app_for(database_url), base_url=BASE, cookies=cookies) as c:
        return c.get(f"/notifications?farm={farm_id}", follow_redirects=False).status_code


def signed_in_member(installation: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    """A farm owner on `installation`, signed in: (login, password, session cookies)."""
    login, password = f"farmer{uuid.uuid4().hex[:6]}", secrets.token_urlsafe(18)
    created = run_cli(
        "users", "create", login, "--farm-id", "1", "--password-stdin",
        env={"DATABASE_URL": installation["DATABASE_URL"]}, input=password + "\n",
    )  # fmt: skip
    assert created.exit_code == 0, created.output
    cookies = sign_in(installation["DATABASE_URL"], login, password)
    assert member_status(installation["DATABASE_URL"], cookies) == 200
    return login, password, cookies


def sigterm(_target: str | None = None) -> None:
    os.kill(os.getpid(), signal.SIGTERM)  # `docker compose stop` at this very moment


def old_of(dbs: Databases, target: str) -> str:
    (old,) = [d for d in staging_of(dbs, target) if "_old_" in d]
    return old
