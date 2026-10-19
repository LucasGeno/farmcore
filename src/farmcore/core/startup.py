"""Startup checks run by `create_app` before anything else is built, and the installation id."""

from __future__ import annotations

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
# nor change it (ALTER DATABASE needs the owner). Copies of the database never inherit it:
# CREATE DATABASE ... TEMPLATE and a plain pg_dump/pg_restore do not carry database settings.
INSTALLATION_SETTING = "farmcore.installation_id"

_READ_INSTALLATION_ID = text(
    "SELECT substr(cfg, length(:name) + 2) FROM pg_db_role_setting s "
    "JOIN pg_database d ON d.oid = s.setdatabase, unnest(s.setconfig) AS cfg "
    "WHERE s.setrole = 0 AND d.datname = current_database() AND split_part(cfg, '=', 1) = :name"
)


def read_installation_id(conn: Connection | Session) -> str | None:
    """This database's installation id, or None when `farmcore migrate` never ran on it.

    Read from the catalog rather than `current_setting()`, which a session could override."""
    return conn.execute(_READ_INSTALLATION_ID, {"name": INSTALLATION_SETTING}).scalar()
