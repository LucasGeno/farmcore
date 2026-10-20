"""`farmcore restore BUNDLE --identity KEY`: restore a backup as a new, independent installation.

A bundle is data only. age keeps it confidential, but anyone holding the owner's public key
(it sits in the server's environment) can encrypt a bundle of their own, so nothing in a bundle
is trusted to run: its schema, functions, views and every other SQL statement are ignored, and
its rows are loaded by a role that can do nothing else.

1. Check the files can be read, decrypt the whole bundle into a private temporary directory
   (age authenticates every chunk: a wrong key, a modified byte or a truncated file fails
   here), and read exactly two plain members, never writing more than their headers declare.
2. Check the manifest (format, fields, dump size and SHA-256, release, apps), that the schema
   revision inside the dump is the one the manifest names, and that this release knows it.
3. Check the target: run as the server's superuser, and the database holds no data of its own
   (only extension objects, as a new installation's database does). Restore never replaces an
   installation.
4. Build the copy in a staging database made from the target (same extensions), closed to
   every other role, with external effects paused. As the owner, create FarmCore's schema at
   the bundle's revision from this release's own migrations. Compare the dump's table of
   contents with a dump of that schema: any object FarmCore's schema does not have (a
   function, trigger, view, table, column default, database setting...) refuses the bundle.
5. Load only rows (`TABLE DATA`) and sequence positions (`SEQUENCE SET`) of FarmCore's own
   tables, never member sessions or sign-in throttling, with `pg_restore --data-only` connected
   as a temporary login role that may only insert into those tables and set those sequences:
   not a superuser, no membership, no CREATE or TEMP anywhere. Foreign keys are dropped for the
   load and added back as the owner, which checks every row.
6. As the owner: empty the session tables again, pause effects again, drop the loader role,
   check the target again, then swap the staging database in under the target's name and run
   the `migrate` steps (upgrade to this release's schema, a new installation id, the copy's
   own runtime role). Those migrations run as the superuser over restored rows, which are as
   untrusted as the bundle: a migration must never put a row's value into SQL text.

One restore runs per target at a time (an advisory lock in `postgres`); it first removes what a
killed restore into the same target left behind. Any failure before the swap, SIGTERM and
SIGHUP included, drops the staging database and the loader role: the target, the source
installation and every bundle are left as they were, and no plaintext is left behind.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import typer
from alembic import command
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

from farmcore.core import registry
from farmcore.core.commands.backup import (
    APP_PACKAGES,
    DUMP_NAME,
    FORMAT,
    MANIFEST_NAME,
    BundleError,
    database_error,
    libpq_env,
    tool,
    tool_error,
)
from farmcore.core.commands.effects import set_external_effects
from farmcore.core.commands.migrate import (
    MigrateError,
    alembic_config,
    check_owner,
    migrate_installation,
)
from farmcore.core.startup import EFFECTS_PAUSED
from farmcore.settings import Settings, get_settings

MANIFEST_MAX_BYTES = 1 << 20
_REQUIRED = {
    "format": str,
    "installation_id": str,
    "release": str,
    "modules": dict,
    "alembic_heads": list,
    "created_at": str,
    "dump": dict,
}
# Relations outside the system schemas that no extension owns: the target's own data.
_OWN_RELATIONS = text(
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
    "AND n.nspname NOT LIKE 'pg_temp%' AND n.nspname NOT LIKE 'pg_toast_temp%' "
    "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND NOT EXISTS (SELECT 1 FROM pg_depend d "
    "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
)
# FarmCore's own tables and sequences in `public` (not an extension's): what a load may fill.
_FARMCORE_RELATIONS = text(
    "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S') AND NOT EXISTS (SELECT 1 FROM "
    "pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e') "
    "ORDER BY 1"
)
_FOREIGN_KEYS = text(
    "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE contype = 'f' AND connamespace = 'public'::regnamespace ORDER BY 1, 2"
)
# Never loaded: the copy starts with no member session and fresh sign-in throttling, and its
# schema revision is the one restore created.
NOT_LOADED = frozenset({"local_session", "login_bucket", "alembic_version"})
PLAIN_FILES = (tarfile.REGTYPE, tarfile.AREGTYPE)
LOADER_PREFIX = "farmcore_restore_"
# Emptied again after the load (an older revision may not have them yet).
SESSION_TABLES = ("local_session", "login_bucket")
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# What `farmcore backup` writes (`datetime.isoformat()` in UTC): nothing else is printed.
_CREATED_AT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})", re.A
)
TEMP_PREFIX = "farmcore-restore-"
# A restore's temporary directory older than this was left by a killed one (the loader role,
# which every load needs, is valid for six hours).
STALE_TEMP_SECONDS = 6 * 3600


class Interrupted(BaseException):
    """SIGTERM or SIGHUP during a restore: unwinds like Ctrl-C, through every cleanup."""


class _Stops:
    """What the stop-signal handler does: raise at once, or note the signal for later."""

    deferring = False
    pending: str | None = None


_STOPS = _Stops()


@contextlib.contextmanager
def _stop_on_signals() -> Iterator[None]:
    def stop(signum, _frame) -> None:
        for s in STOP_SIGNALS:  # one is enough; let the cleanup finish
            signal.signal(s, signal.SIG_IGN)
        name = signal.Signals(signum).name
        if _STOPS.deferring:
            _STOPS.pending = name
            return
        raise Interrupted(name)

    _STOPS.deferring, _STOPS.pending = False, None
    previous = {s: signal.signal(s, stop) for s in STOP_SIGNALS}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


@contextlib.contextmanager
def _signals_deferred() -> Iterator[None]:
    """A stop signal that arrives while the block runs raises when it ends, not inside it.

    A flag, not a signal mask: a mask holds a signal back from this thread only, and the
    kernel may hand a process's signal to any other thread, whose handler call Python then
    runs here at once."""
    _STOPS.deferring = True
    try:
        yield
    finally:
        _STOPS.deferring = False
        pending, _STOPS.pending = _STOPS.pending, None
        if pending:
            raise Interrupted(pending)


def sweep_temp() -> None:
    """Remove this user's restore temporary directories that a killed restore left (they hold
    the bundle's rows in clear)."""
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - STALE_TEMP_SECONDS
    for path in root.glob(f"{TEMP_PREFIX}*"):
        info = path.lstat()
        if path.is_dir() and info.st_uid == os.geteuid() and info.st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _printable(value: str) -> str:
    """A bundle's string for the terminal: control characters escaped, never sent raw."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in value)


# --- 1. The files ---------------------------------------------------------------------------


def preflight(bundle: Path, identity: Path) -> None:
    """Fail early, and say which file, when the files cannot be read."""
    hint = 'run restore as a user who can read it (Compose: --user "$(id -u):$(id -g)")'
    try:
        found = bundle.is_file()
    except PermissionError:  # a directory on the way that this user cannot search
        found = None
    if found is False:
        raise BundleError(f"no bundle at {bundle}")
    if not found or not os.access(bundle, os.R_OK):
        raise BundleError(f"cannot read the bundle {bundle}: {hint}")
    try:
        readable = identity.is_file() and os.access(identity, os.R_OK)
    except PermissionError:
        readable = False
    if not readable:
        raise BundleError(f"cannot read the key file {identity}: {hint}")
    with bundle.open("rb") as f:
        if f.read(5) == b"PGDMP":
            raise BundleError(
                f"{bundle} looks like a plain pg_dump from an older release, not a bundle; see "
                '"Upgrading" in docs/self-hosting.md for restoring it'
            )


def decrypt(bundle: Path, identity: Path, out: Path) -> None:
    done = subprocess.run(
        [tool("age"), "-d", "-i", str(identity), "-o", str(out), "--", str(bundle)],
        capture_output=True,
    )
    if done.returncode != 0:
        raise BundleError(
            "cannot decrypt the bundle (wrong key, or the file is modified or truncated): "
            + tool_error(done.stderr)
        )


def unpack(plain: Path, into: Path) -> tuple[dict, Path]:
    """The manifest and the dump's path. Exactly two plain files, each no larger than the
    archive itself, copied with a byte count: links, devices, sparse files and members that
    claim more than they carry are refused before anything is written."""
    available = plain.stat().st_size
    try:
        with tarfile.open(plain, mode="r:") as tar:
            found = tar.getmembers()
            if [m.name for m in found] != [MANIFEST_NAME, DUMP_NAME] or not all(
                m.type in PLAIN_FILES and m.sparse is None and 0 <= m.size <= available
                for m in found
            ):
                raise BundleError("not a FarmCore backup: unexpected contents")
            if found[0].size > MANIFEST_MAX_BYTES:
                raise BundleError("not a FarmCore backup: manifest too large")
            raw = _read(tar, found[0], None)
            dump = into / DUMP_NAME
            with dump.open("wb") as dst:
                _read(tar, found[1], dst)
    except tarfile.TarError:
        raise BundleError("not a FarmCore backup: unreadable archive") from None
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise BundleError("not a FarmCore backup: unreadable manifest") from None
    return manifest, dump


def _read(tar: tarfile.TarFile, member: tarfile.TarInfo, dst) -> bytes:
    """A member's bytes (into `dst` when given), never more than its header declares."""
    src = tar.extractfile(member)
    if src is None:  # pragma: no cover - only plain files reach here
        raise BundleError("not a FarmCore backup: unexpected contents")
    kept, total = [], 0
    with src:
        while chunk := src.read(min(1 << 20, member.size - total + 1)):
            total += len(chunk)
            if total > member.size:  # pragma: no cover - tarfile reads exactly `size`
                raise BundleError("not a FarmCore backup: member larger than declared")
            if dst is None:
                kept.append(chunk)
            else:
                dst.write(chunk)
    if total != member.size:  # pragma: no cover - tarfile raises on a short member first
        raise BundleError("not a FarmCore backup: truncated member")
    return b"".join(kept)


# --- 2. The manifest -------------------------------------------------------------------------


def _release(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+){0,3}", value):
        raise BundleError("manifest field 'release' is missing or invalid")
    return tuple(int(part) for part in value.split("."))


def check_manifest(manifest: object, dump: Path) -> dict:
    if not isinstance(manifest, dict):
        raise BundleError("not a FarmCore backup: unreadable manifest")
    if manifest.get("format") != FORMAT:
        raise BundleError(f"unsupported bundle format {manifest.get('format')!r}")
    for key, kind in _REQUIRED.items():
        if not isinstance(manifest.get(key), kind):
            raise BundleError(f"manifest field {key!r} is missing or invalid")
    # Both are printed after the restore: only what they claim to be.
    try:
        canonical = str(uuid.UUID(manifest["installation_id"])) == manifest["installation_id"]
    except ValueError:
        canonical = False
    if not canonical:
        raise BundleError("manifest field 'installation_id' is missing or invalid")
    # `datetime.fromisoformat` alone takes any one character between date and time.
    try:
        valid = bool(_CREATED_AT.fullmatch(manifest["created_at"]))
        datetime.fromisoformat(manifest["created_at"])
    except ValueError:
        valid = False
    if not valid:
        raise BundleError("manifest field 'created_at' is missing or invalid")
    meta = manifest["dump"]
    digest = hashlib.sha256()
    with dump.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if (
        meta.get("name") != DUMP_NAME
        or meta.get("bytes") != dump.stat().st_size
        or meta.get("sha256") != digest.hexdigest()
    ):
        raise BundleError("the dump does not match the manifest (size or SHA-256)")
    heads = manifest["alembic_heads"]
    if not heads or not all(isinstance(h, str) for h in heads):
        raise BundleError("manifest field 'alembic_heads' is missing or invalid")
    ours = version("farmcore")
    if _release(manifest["release"]) > _release(ours):
        raise BundleError(
            f"the bundle comes from a newer FarmCore release ({manifest['release']}) than this "
            f"one ({ours}); restore it with that release or a later one"
        )
    known = {m.name for m in registry.discover(packages=APP_PACKAGES)}
    unknown = sorted(set(manifest["modules"]) - known)
    if unknown:
        shown = ", ".join(_printable(name) for name in unknown)
        raise BundleError(f"the bundle has apps this release does not ship: {shown}")
    return manifest


def _dump_heads(dump: Path) -> list[str]:
    """The schema revisions recorded inside the dump itself (its `alembic_version` rows).

    `pg_restore -f -` only reads the archive and prints it; nothing is executed."""
    data = subprocess.run(
        [tool("pg_restore"), "--data-only", "--table=alembic_version", "-f", "-", str(dump)],
        capture_output=True,
    )
    if data.returncode != 0:
        raise BundleError(f"the dump is unreadable: {tool_error(data.stderr)}")
    out = data.stdout.decode(errors="replace")
    rows = re.search(r"^COPY public\.alembic_version .*?;\n(.*?)^\\\.$", out, re.M | re.S)
    return sorted(rows.group(1).split()) if rows else []


def check_compatible(manifest: dict, dump: Path) -> list[str]:
    heads = sorted(manifest["alembic_heads"])
    if _dump_heads(dump) != heads:
        raise BundleError("the dump's schema revision does not match the manifest")
    script = ScriptDirectory.from_config(alembic_config())
    for head in heads:
        try:
            known = script.get_revision(head)
        except CommandError:
            known = None
        if known is None:
            raise BundleError(
                f"incompatible release: the bundle comes from FarmCore {manifest['release']} "
                f"(schema {', '.join(map(_printable, heads))}), which this release does not "
                "know; restore it "
                "with that release or a later one"
            )
    return heads


# --- 3. The target ---------------------------------------------------------------------------


def check_target(settings: Settings) -> str:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            me = check_owner(conn)
            own = conn.execute(_OWN_RELATIONS).scalar_one()
    finally:
        engine.dispose()
    if own:
        database = make_url(settings.database_url).database
        raise BundleError(
            f"database {database} already holds data ({own} relations); restore only into a new "
            "installation's empty database (start only its `db` service)"
        )
    return me


# --- 4-6. The copy ----------------------------------------------------------------------------


def _toc(listing: str) -> list[tuple[str, str]]:
    """`pg_restore --list` lines as (line, key): the key is the entry's kind, schema and name
    without its ids and owner, so two dumps of one schema have the same keys."""
    entries = []
    for line in listing.splitlines():
        match = re.match(r"^\d+; \d+ \d+ (.*)$", line)
        if match:
            entries.append((line, match.group(1).rsplit(" ", 1)[0]))
    return entries


def _list(dump: Path) -> list[tuple[str, str]]:
    listed = subprocess.run([tool("pg_restore"), "--list", str(dump)], capture_output=True)
    if listed.returncode != 0:
        raise BundleError(f"the dump is unreadable: {tool_error(listed.stderr)}")
    return _toc(listed.stdout.decode(errors="replace"))


def _safe_load_error(stderr: bytes) -> str:
    """pg_restore's error without the value it quotes (`...: "<row value>"`, quotes inside
    included: everything from the first quoted part on), so a failed load never prints a row."""
    line = tool_error(stderr)
    return re.sub(r':\s*".*$', "", line, flags=re.S)


class Copy:
    """The staging database a restore builds, and the loader role that fills it."""

    def __init__(self, settings: Settings, owner: str) -> None:
        self.settings = settings
        self.owner = owner
        self.url = make_url(settings.database_url)
        suffix = uuid.uuid4().hex[:8]
        database = self.url.database
        # Names fit PostgreSQL's 63 bytes and still tell targets apart that share a prefix.
        self.short = f"{database[:32]}_{hashlib.sha256(database.encode()).hexdigest()[:6]}"
        self.stem = f"{database[:32]}_restore_{self.short[-6:]}_"
        self.name = self.stem + suffix
        self.staging = self.url.set(database=self.name)
        self.loader = f"{LOADER_PREFIX}{suffix}"
        self.loader_password = secrets.token_urlsafe(32)
        self.created = self.loader_created = False
        self.present: set[str] = set()
        self.tables: list[str] = []
        self.sequences: list[str] = []

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold this target's restore lock (a session lock in `postgres`) while the block runs."""
        engine = self._admin()
        try:
            with engine.connect() as conn:
                got = conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
                    {"k": f"farmcore.restore:{self.url.database}"},
                ).scalar_one()
                if not got:
                    raise BundleError(
                        f"another restore into {self.url.database} is running on this server; "
                        "wait for it to finish, then try again"
                    )
                yield  # closing the connection releases the lock
        finally:
            engine.dispose()

    def sweep(self) -> None:
        """Drop what a killed restore into this target left: its staging databases (made by
        this owner) and their loader roles, and loader roles whose staging database is gone."""
        stale = re.compile(re.escape(self.stem) + r"([0-9a-f]{8})")
        loader = re.compile(re.escape(LOADER_PREFIX) + r"([0-9a-f]{8})")
        engine = self._admin()
        try:
            with engine.connect() as conn:
                # Roles first: a loader role is created after its database, so a role listed
                # here whose database is missing from the next list has lost it.
                roles = conn.execute(text("SELECT rolname FROM pg_roles")).scalars().all()
                loaders = {m.group(1): r for r in roles if (m := loader.fullmatch(r))}
                names = conn.execute(text("SELECT datname FROM pg_database")).scalars().all()
                alive = {m.group(1) for n in names if (m := _STAGING_SUFFIX.search(n))}
                mine = conn.execute(
                    text(
                        "SELECT datname FROM pg_database "
                        "WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                    )
                ).scalars()
                for name in mine.all():
                    if match := stale.fullmatch(name):
                        conn.exec_driver_sql(
                            f"DROP DATABASE IF EXISTS {_q(conn, name)} WITH (FORCE)"
                        )
                        alive.discard(match.group(1))
                for suffix, role in loaders.items():
                    if suffix not in alive:
                        conn.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(conn, role)}")
        finally:
            engine.dispose()

    def _admin(self):
        return create_engine(self.url.set(database="postgres"), isolation_level="AUTOCOMMIT")

    def _url(self, url: URL) -> str:
        return url.render_as_string(hide_password=False)

    def create(self) -> None:
        engine = self._admin()
        try:
            with engine.connect() as conn:
                q = conn.dialect.identifier_preparer.quote
                conn.exec_driver_sql(
                    f"CREATE DATABASE {q(self.name)} OWNER {q(self.owner)} "
                    f"TEMPLATE {q(self.url.database)}"
                )
                self.created = True
                conn.exec_driver_sql(f"REVOKE ALL ON DATABASE {q(self.name)} FROM PUBLIC")
                # PUBLIC may open, and create temporary tables in, any new database; the loader
                # must reach only the staging one (`migrate` revokes the same).
                others = conn.execute(
                    text("SELECT datname FROM pg_database WHERE datname NOT IN ('template0', :s)"),
                    {"s": self.name},
                ).scalars()
                for other in others.all():
                    conn.exec_driver_sql(
                        f"REVOKE CONNECT, TEMPORARY ON DATABASE {q(other)} FROM PUBLIC"
                    )
        finally:
            engine.dispose()
        engine = create_engine(self._url(self.staging))
        try:
            with engine.begin() as conn:
                set_external_effects(conn, EFFECTS_PAUSED)
                conn.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        finally:
            engine.dispose()

    def build_schema(self, heads: list[str]) -> None:
        cfg = alembic_config(self._url(self.staging))
        for head in heads:
            command.upgrade(cfg, head)

    def check_contents(self, dump: Path, tmp: Path) -> list[str]:
        """The dump's data entries to load; refuse anything FarmCore's schema lacks."""
        expected_dump = tmp / "schema.dump"
        done = subprocess.run(
            [tool("pg_dump"), "-Fc", "--no-acl", "-f", str(expected_dump)],
            env=libpq_env(self._url(self.staging)), capture_output=True,
        )  # fmt: skip
        if done.returncode != 0:
            raise BundleError(f"pg_dump of the new schema failed: {tool_error(done.stderr)}")
        expected = {key for _, key in _list(expected_dump)}
        entries = _list(dump)
        extra = sorted({key for _, key in entries} - expected)
        if extra:
            more = f"; and {len(extra) - 5} more" if len(extra) > 5 else ""
            shown = "; ".join(map(_printable, extra[:5])) + more
            raise BundleError(f"the dump holds objects that are not part of FarmCore: {shown}")
        engine = create_engine(self._url(self.staging))
        try:
            with engine.connect() as conn:
                relations = conn.execute(_FARMCORE_RELATIONS).all()
        finally:
            engine.dispose()
        self.present = {n for n, _ in relations}
        self.tables = sorted(n for n, kind in relations if kind == "r" and n not in NOT_LOADED)
        self.sequences = sorted(n for n, kind in relations if kind == "S")
        loadable = {f"TABLE DATA public {t}" for t in self.tables}
        loadable |= {f"SEQUENCE SET public {s}" for s in self.sequences}
        return [line for line, key in entries if key in loadable]

    def create_loader(self) -> None:
        engine = create_engine(self._url(self.staging))
        try:
            with engine.begin() as conn:
                raw = conn.connection.driver_connection
                verifier = raw.pgconn.encrypt_password(
                    self.loader_password.encode(), self.loader.encode(), b"scram-sha-256"
                ).decode()
                # The server's clock, not this machine's.
                until = conn.execute(text("SELECT (now() + interval '6 hours')::text")).scalar_one()
                r = sql.Identifier(self.loader)
                statements = [
                    sql.SQL(
                        "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                        "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2 VALID UNTIL {} PASSWORD {}"
                    ).format(r, sql.Literal(until), sql.Literal(verifier)),
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(self.name), r
                    ),
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r),
                ]
                statements += [
                    sql.SQL("GRANT INSERT ON TABLE public.{} TO {}").format(sql.Identifier(t), r)
                    for t in self.tables
                ]
                statements += [
                    sql.SQL("GRANT USAGE, UPDATE ON SEQUENCE public.{} TO {}").format(
                        sql.Identifier(s), r
                    )
                    for s in self.sequences
                ]
                for i, statement in enumerate(statements):
                    raw.execute(statement)
                    if i == 0:
                        self.loader_created = True
        finally:
            engine.dispose()

    def load(self, dump: Path, lines: list[str], tmp: Path) -> None:
        engine = create_engine(self._url(self.staging))
        try:
            with engine.begin() as conn:
                foreign_keys = conn.execute(_FOREIGN_KEYS).all()
                for table, name, _ in foreign_keys:
                    conn.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {_q(conn, name)}")
        finally:
            engine.dispose()
        entries = tmp / "load.list"
        entries.write_text("".join(line + "\n" for line in lines))
        loader_url = self.staging.set(username=self.loader, password=self.loader_password)
        done = subprocess.run(
            [tool("pg_restore"), "--data-only", "--no-owner", "--no-acl", "--exit-on-error",
             "--single-transaction", "--use-list", str(entries), "-d", self.name, str(dump)],
            env=libpq_env(self._url(loader_url)), capture_output=True,
        )  # fmt: skip
        if done.returncode != 0:
            raise BundleError(f"pg_restore failed: {_safe_load_error(done.stderr)}")
        engine = create_engine(self._url(self.staging))
        try:
            with engine.connect() as conn:
                for table, name, definition in foreign_keys:
                    try:
                        with conn.begin():
                            conn.exec_driver_sql(
                                f"ALTER TABLE {table} ADD CONSTRAINT {_q(conn, name)} {definition}"
                            )
                    except DBAPIError:
                        raise BundleError(
                            f"the bundle's rows break FarmCore's constraint {name}"
                        ) from None
                with conn.begin():
                    sessions = [t for t in SESSION_TABLES if t in self.present]
                    if sessions:
                        conn.exec_driver_sql(f"TRUNCATE {', '.join(sessions)}")
                    set_external_effects(conn, EFFECTS_PAUSED)
        finally:
            engine.dispose()

    def drop_loader(self) -> None:
        """DROP OWNED (its grants, and anything it created) here, then the role."""
        engine = create_engine(self._url(self.staging))
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP OWNED BY {_q(conn, self.loader)}")
        finally:
            engine.dispose()
        engine = self._admin()
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql(f"DROP ROLE {_q(conn, self.loader)}")
        finally:
            engine.dispose()
        self.loader_created = False

    def swap(self) -> None:
        """The staging database takes the target's name; the empty target is dropped."""
        old = f"{self.url.database[:32]}_old_{self.short[-6:]}_{uuid.uuid4().hex[:8]}"
        engine = self._admin()
        try:
            with engine.connect() as conn:
                q = conn.dialect.identifier_preparer.quote
                conn.exec_driver_sql(f"ALTER DATABASE {q(self.url.database)} RENAME TO {q(old)}")
                try:
                    conn.exec_driver_sql(
                        f"ALTER DATABASE {q(self.name)} RENAME TO {q(self.url.database)}"
                    )
                except BaseException:
                    conn.exec_driver_sql(
                        f"ALTER DATABASE {q(old)} RENAME TO {q(self.url.database)}"
                    )
                    raise
                self.created = False
                conn.exec_driver_sql(f"DROP DATABASE {q(old)} WITH (FORCE)")
        finally:
            engine.dispose()

    def discard(self) -> None:
        """After a failure: no staging database and no loader role are left."""
        engine = self._admin()
        try:
            with engine.connect() as conn:
                if self.created:
                    name = _q(conn, self.name)
                    conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
                if self.loader_created:
                    conn.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(conn, self.loader)}")
        finally:
            engine.dispose()


_STAGING_SUFFIX = re.compile(r"_restore_[0-9a-f]{6}_([0-9a-f]{8})$")


def _q(conn, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def restore_bundle(settings: Settings, bundle: Path, identity: Path) -> tuple[dict, str]:
    """Verify `bundle` completely, then restore its rows into the empty target as a new
    installation; (manifest, new installation id)."""
    sweep_temp()
    preflight(bundle, identity)
    with (
        _stop_on_signals(),
        tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as name,
    ):
        tmp = Path(name)
        plain = tmp / "bundle.tar"
        decrypt(bundle, identity, plain)
        manifest, dump = unpack(plain, tmp)
        plain.unlink()
        check_manifest(manifest, dump)
        heads = check_compatible(manifest, dump)
        copy = Copy(settings, check_target(settings))
        with copy.exclusive():
            copy.sweep()
            swapped = False
            try:
                # Never stopped between the two renames: a signal that arrives during the swap
                # is held, and delivered once this block is left (outside the cleanup below).
                with contextlib.ExitStack() as deferred:
                    try:
                        copy.create()
                        copy.build_schema(heads)
                        lines = copy.check_contents(dump, tmp)
                        copy.create_loader()
                        copy.load(dump, lines, tmp)
                        copy.drop_loader()
                        check_target(settings)  # still empty: nobody started using it meanwhile
                        deferred.enter_context(_signals_deferred())
                        copy.swap()
                        swapped = True
                    except BaseException:
                        copy.discard()
                        raise
                new_id = migrate_installation(settings)
            except Interrupted as exc:
                if not swapped:
                    raise
                raise BundleError(
                    f"stopped by {exc} after the copy was swapped in: the data is in, paused; "
                    "run migrate to finish"
                ) from None
    return manifest, new_id


def register(cli: typer.Typer) -> None:
    @cli.command()
    def restore(
        # Readability is checked by restore itself, with a hint for containers.
        bundle: Path = typer.Argument(
            ..., help="Bundle written by `farmcore backup`.", readable=False
        ),
        identity: Path = typer.Option(
            ...,
            "--identity",
            "-i",
            help="The owner's age identity (private key) file.",
            readable=False,
        ),  # fmt: skip
    ) -> None:
        """Restore a backup into this new installation's empty database (DATABASE_URL, owner)."""
        settings = get_settings()
        try:
            manifest, new_id = restore_bundle(settings, bundle, identity)
        except (BundleError, MigrateError) as exc:
            typer.echo(f"restore: {exc}", err=True)
            raise typer.Exit(1) from None
        except Interrupted as exc:
            typer.echo(
                f"restore: stopped by {exc}; nothing was restored, and the staging copy and its "
                "loader role were removed",
                err=True,
            )
            raise typer.Exit(1) from None
        except DBAPIError as exc:
            typer.echo(f"restore: cannot use the database: {database_error(exc)}", err=True)
            raise typer.Exit(1) from None
        typer.echo(
            f"restore: backup of installation {_printable(manifest['installation_id'])} "
            f"({_printable(manifest['created_at'])}) restored"
        )
        typer.echo(f"installation {new_id}")
