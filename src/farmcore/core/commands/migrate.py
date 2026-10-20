"""`farmcore migrate`: upgrade the database to the latest revision and give the installation
its identity. Run as the database owner; the app and bot never migrate."""

from __future__ import annotations

import uuid
from importlib.resources import files

import psycopg
import typer
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Connection, create_engine, text

from farmcore.core.startup import INSTALLATION_SETTING, read_installation_id
from farmcore.settings import get_settings


def alembic_config(url: str | None = None) -> Config:
    """Alembic config built in code (works without `alembic.ini`, e.g. in the container)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(files("farmcore") / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def ensure_installation_id(conn: Connection) -> str:
    """Generate the installation id once (random, never user-chosen); return the stored one."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": INSTALLATION_SETTING})
    existing = read_installation_id(conn)
    if existing:
        return existing
    new = str(uuid.uuid4())
    database = conn.dialect.identifier_preparer.quote(
        conn.execute(text("SELECT current_database()")).scalar_one()
    )
    me = conn.execute(text("SELECT current_user")).scalar_one()
    # A UUID string is a safe literal; ALTER DATABASE takes no bind parameters.
    try:
        # psycopg directly, so the one error handled here arrives as itself.
        conn.connection.driver_connection.execute(
            f"ALTER DATABASE {database} SET {INSTALLATION_SETTING} = '{new}'"
        )
    except psycopg.errors.InsufficientPrivilege:
        # PostgreSQL 15+: only a superuser, or a role granted SET on it, may store a custom
        # setting, even on a database it owns (a shared server's ordinary owner role).
        raise MigrateError(
            f"{me} may not store the installation id. As a superuser, run once: "
            f"GRANT SET ON PARAMETER {INSTALLATION_SETTING} TO {me}"
        ) from None
    return new


# Tables the runtime role may read but never write.
READ_ONLY_TABLES = ("alembic_version", "spatial_ref_sys")
RUNTIME_ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
# Databases every PostgreSQL server has (template_postgis: PostGIS images). PUBLIC can connect
# and create temporary tables in them by default; the runtime role must not.
SERVER_DATABASES = ("postgres", "template1", "template_postgis")


class MigrateError(RuntimeError):
    """A refusal or failure `farmcore migrate` reports in one line (never with SQL text)."""


def check_owner(conn: Connection) -> str:
    """The connected role, which must own this database (or be a superuser); else refuse."""
    me, owner, superuser = conn.execute(
        text(
            "SELECT current_user, pg_get_userbyid(d.datdba), r.rolsuper FROM pg_database d, "
            "pg_roles r WHERE d.datname = current_database() AND r.rolname = current_user"
        )
    ).one()
    if me != owner and not superuser:
        raise MigrateError(
            f"farmcore migrate must run as the database owner ({owner}), not as {me}: use the "
            "owner's DATABASE_URL (POSTGRES_PASSWORD), as the compose `migrate` service does. "
            "The app's runtime role cannot change the schema."
        )
    return me


def ensure_runtime_role(conn: Connection, role: str, password: str) -> None:
    """Create or reset the DML-only runtime role and grant it this database, nothing more.

    The owner (the connected role) keeps the schema, the PostGIS extension and every DDL right.
    The runtime role may connect to this database only, use the `public` schema, read and write
    rows and use sequences; default privileges extend that to tables later migrations add.

    An existing role is reset first: every role membership (whoever granted it), role-level
    setting and directly granted privilege on any database, or on this database's schemas,
    tables, sequences and functions, is removed before the intended grants are made again. A
    role that owns any object is refused: ownership carries every right on that object.

    Statements go through psycopg directly, with the password sent as a SCRAM verifier computed
    here: the plaintext is never part of any statement, and a failure reports the server's
    message only, never the statement."""
    me = conn.execute(text("SELECT current_user")).scalar_one()
    if role == me:
        raise MigrateError(
            f"RUNTIME_DATABASE_ROLE must differ from the owner role ({me}): the runtime role is "
            "the restricted one."
        )
    raw = conn.connection.driver_connection
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "farmcore.runtime"})
    database_name = conn.execute(text("SELECT current_database()")).scalar_one()
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    if exists:
        owned = conn.execute(_OWNED, {"r": role}).scalar_one()
        if owned:
            raise MigrateError(
                f"{role} owns {owned} database object(s); the runtime role must own nothing. "
                f"Reassign them (REASSIGN OWNED BY {role} TO {me}) and run migrate again."
            )

    def names(query: str, **params: object) -> list[str]:
        return list(conn.execute(text(query), params).scalars())

    # Databases whose privileges this role may change: all of them as superuser, else its own.
    manageable = (
        "SELECT d.datname FROM pg_database d, pg_roles r WHERE r.rolname = current_user "
        "AND d.datallowconn AND (r.rolsuper OR d.datdba = r.oid)"
    )
    server_dbs = [d for d in names(manageable) if d in SERVER_DATABASES]
    all_dbs = names(manageable)
    schemas = names(
        "SELECT nspname FROM pg_namespace WHERE nspname <> 'information_schema' "
        "AND nspname NOT LIKE 'pg\\_%'"
    )
    r = sql.Identifier(role)
    owner = sql.Identifier(me)
    database = sql.Identifier(database_name)
    verifier = raw.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256")
    verb = "ALTER ROLE {} WITH " if exists else "CREATE ROLE {} WITH "
    statements = [
        sql.SQL(verb + RUNTIME_ATTRIBUTES + " PASSWORD {}").format(
            r, sql.Literal(verifier.decode())
        )
    ]
    for granted, grantor in conn.execute(_MEMBERSHIPS, {"r": role}).all():
        statements.append(
            sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
                sql.Identifier(granted), r, sql.Identifier(grantor)
            )
        )
    statements += [
        sql.SQL("ALTER ROLE {} RESET ALL").format(r),
        sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(r, database),
    ]
    statements += [
        sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(d))
        for d in server_dbs
    ]
    statements += [
        sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(d), r) for d in all_dbs
    ]
    for schema in schemas:
        s = sql.Identifier(schema)
        for kind in ("SCHEMA {}", "ALL TABLES IN SCHEMA {}", "ALL SEQUENCES IN SCHEMA {}",
                     "ALL FUNCTIONS IN SCHEMA {}"):  # fmt: skip
            statements.append(sql.SQL("REVOKE ALL ON " + kind + " FROM {}").format(s, r))
    for statement in (
        "REVOKE ALL ON DATABASE {db} FROM PUBLIC",
        "GRANT CONNECT ON DATABASE {db} TO {r}",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "GRANT USAGE ON SCHEMA public TO {r}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}",
        "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
        "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO {r}",
    ):
        statements.append(sql.SQL(statement).format(db=database, r=r, owner=owner))
    for table in READ_ONLY_TABLES:
        statements.append(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {} FROM {}").format(sql.Identifier(table), r)
        )
    try:
        for statement in statements:
            raw.execute(statement)
    except psycopg.Error as exc:
        detail = exc.diag.message_primary or type(exc).__name__
        raise MigrateError(f"could not set up the runtime role {role}: {detail}") from None
    left = [g for g, _ in conn.execute(_MEMBERSHIPS, {"r": role}).all()]
    if left:
        raise MigrateError(
            f"{role} is still a member of {', '.join(sorted(set(left)))} after revoking; remove "
            "those memberships by hand and run migrate again."
        )


# Objects a role owns, in any database of the server.
_OWNED = text(
    "SELECT count(*) FROM pg_shdepend WHERE deptype = 'o' "
    "AND refclassid = 'pg_authid'::regclass AND refobjid = (SELECT oid FROM pg_roles "
    "WHERE rolname = :r)"
)
# Every membership of a role, with who granted it (a REVOKE removes only one grantor's grant).
_MEMBERSHIPS = text(
    "SELECT g.rolname, gr.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
    "JOIN pg_roles u ON u.oid = m.member JOIN pg_roles gr ON gr.oid = m.grantor "
    "WHERE u.rolname = :r"
)


def register(cli: typer.Typer) -> None:
    @cli.command()
    def migrate() -> None:
        """Upgrade the database schema to head and set up the installation (DATABASE_URL).

        Run as the database owner; the runtime role is refused."""
        settings = get_settings()
        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as conn:
                check_owner(conn)
            command.upgrade(alembic_config(settings.database_url), "head")
            with engine.begin() as conn:
                installation_id = ensure_installation_id(conn)
                if settings.runtime_database_password:
                    ensure_runtime_role(
                        conn,
                        settings.runtime_database_role,
                        settings.runtime_database_password.get_secret_value(),
                    )
        except MigrateError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        finally:
            engine.dispose()
        typer.echo("migrated to head")
        typer.echo(f"installation {installation_id}")
