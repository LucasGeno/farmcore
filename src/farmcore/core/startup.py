"""Startup checks run by `create_app` before anything else is built, and the installation id."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from importlib.resources import files

from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from farmcore.settings import Settings

MIN_TOKEN_CHARS = 32


def check_public_settings(settings: Settings) -> None:
    """Refuse a public deployment without a strong admin token.

    Private reads fail closed without a token. Network deployments additionally require a
    strong operator token for bootstrap. The message names variables only, never a value.
    """
    if not settings.farmcore_public:
        return
    token = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if len(token) < MIN_TOKEN_CHARS or any(c.isspace() for c in token):
        raise RuntimeError(
            f"FARMCORE_PUBLIC=1 needs ADMIN_TOKEN: at least {MIN_TOKEN_CHARS} characters, no "
            "whitespace (e.g. openssl rand -hex 24), or unset FARMCORE_PUBLIC for local development"
        )


# The installation's identity is a database-level setting written once by `farmcore migrate`
# (as the database owner): no table, so no schema migration, and a runtime role can neither set
# nor change it (ALTER DATABASE needs the owner). It travels only with database settings: a
# copied data directory, pg_dumpall and `pg_dump --create` + `pg_restore --create` keep it;
# CREATE DATABASE ... TEMPLATE and a plain pg_dump/pg_restore do not, so `migrate` makes a new one.
INSTALLATION_SETTING = "farmcore.installation_id"

_READ_DATABASE_SETTING = text(
    "SELECT substr(cfg, length(:name) + 2) FROM pg_db_role_setting s "
    "JOIN pg_database d ON d.oid = s.setdatabase, unnest(s.setconfig) AS cfg "
    "WHERE s.setrole = 0 AND d.datname = current_database() AND split_part(cfg, '=', 1) = :name"
)


def read_installation_id(conn: Connection | Session) -> str | None:
    """This database's installation id, or None when `farmcore migrate` never ran on it.

    Read from the catalog rather than `current_setting()`, which a session could override."""
    return conn.execute(_READ_DATABASE_SETTING, {"name": INSTALLATION_SETTING}).scalar()


# Whether this installation may act outside itself (today: the Telegram advice push, and the
# bot's long polling). A database-level setting like the id, so no schema change and only the
# owner can switch it (`farmcore effects`). `farmcore restore` sets it to `paused` before any
# data arrives, so two copies of one installation never both send. Unset (every installation
# that was never restored) means enabled.
EFFECTS_SETTING = "farmcore.external_effects"
EFFECTS_ENABLED = "enabled"
EFFECTS_PAUSED = "paused"


def read_external_effects(conn: Connection | Session) -> str:
    """`enabled` or `paused`; anything but an explicit `enabled` or no setting is paused."""
    value = conn.execute(_READ_DATABASE_SETTING, {"name": EFFECTS_SETTING}).scalar()
    return EFFECTS_ENABLED if value in (None, EFFECTS_ENABLED) else EFFECTS_PAUSED


# --- Schema state: what `/health` and `farmcore upgrade` report --------------------------------

# This release's Alembic scripts (a module attribute, read at call time).
MIGRATIONS = files("farmcore") / "migrations"


@functools.cache
def _revisions(location: str) -> tuple[tuple[str, ...], frozenset[str]]:
    script = ScriptDirectory(location)
    return tuple(sorted(script.get_heads())), frozenset(r.revision for r in script.walk_revisions())


def code_revisions() -> tuple[tuple[str, ...], frozenset[str]]:
    """This release's Alembic heads and every revision it knows."""
    return _revisions(str(MIGRATIONS))


# The one migrator: `farmcore migrate` and `farmcore upgrade` hold this session-level advisory
# lock (two int4 keys, "FCOR" "MIGR") on the connection that runs the migrations, in the
# installation database. Anyone may see it in `pg_locks`; it goes with the connection.
# Per installation on purpose (advisory locks are per database): it guards one schema, and a
# slow or stuck upgrade of one installation must never hold up another's. What `migrate` does
# server-wide (the runtime role, PUBLIC's access to other databases) is serialized on its own,
# by locking `pg_database` in that transaction.
MIGRATION_LOCK = (0x46434F52, 0x4D494752)
# Set by the migrator just before it migrates and reset once everything succeeded: still set
# afterwards means the last migrate or upgrade failed or was stopped. A database-level setting
# like the id (owner only, no schema change); a backup never carries it.
UPGRADE_SETTING = "farmcore.upgrade"
UPGRADE_FAILED = "failed"

STATE_OK = "ok"
STATE_MAINTENANCE = "maintenance"
STATE_FAILED = "failed"

_MIGRATION_RUNNING = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid = :k1 AND objid = :k2 AND objsubid = 2 AND pid <> pg_backend_pid())"
)


@dataclass(frozen=True)
class SchemaState:
    """`state` is ok, maintenance (a migration runs or is pending) or failed; `reason` is for
    the operator's terminal, never for `/health`."""

    state: str
    heads: tuple[str, ...]
    expected: tuple[str, ...]
    unknown: tuple[str, ...]
    reason: str


def read_heads(conn: Connection | Session) -> tuple[str, ...]:
    """The database's Alembic heads; none when it was never migrated."""
    if conn.execute(text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
        return ()
    rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars()
    return tuple(sorted(rows))


def read_schema_state(conn: Connection | Session) -> SchemaState:
    """This database as this release sees it. Readable by the runtime role."""
    expected, known = code_revisions()
    heads = read_heads(conn)
    unknown = tuple(h for h in heads if h not in known)
    k1, k2 = MIGRATION_LOCK
    if conn.execute(_MIGRATION_RUNNING, {"k1": k1, "k2": k2}).scalar():
        state, reason = STATE_MAINTENANCE, "a migration is running"
    elif conn.execute(_READ_DATABASE_SETTING, {"name": UPGRADE_SETTING}).scalar():
        state, reason = STATE_FAILED, "the last migrate or upgrade failed or was stopped"
    elif unknown:
        state, reason = STATE_FAILED, "the database has a schema this release does not know"
    elif heads != expected:
        state, reason = STATE_MAINTENANCE, "migrations are pending"
    else:
        state, reason = STATE_OK, "up to date"
    return SchemaState(state, heads, expected, unknown, reason)
