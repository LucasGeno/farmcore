"""`farmcore migrate`: upgrade the database to the latest revision and give the installation
its identity. Run as the database owner; the app and bot never migrate."""

from __future__ import annotations

import uuid
from importlib.resources import files

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
    # A UUID string is a safe literal; ALTER DATABASE takes no bind parameters.
    conn.exec_driver_sql(f"ALTER DATABASE {database} SET {INSTALLATION_SETTING} = '{new}'")
    return new


# Tables the runtime role may read but never write.
READ_ONLY_TABLES = ("alembic_version", "spatial_ref_sys")
RUNTIME_ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"


def ensure_runtime_role(conn: Connection, role: str, password: str) -> None:
    """Create or update the DML-only runtime role and grant it this database, nothing more.

    The owner (the connected role) keeps the schema, the PostGIS extension and every DDL right.
    The runtime role may connect to this database only, use the `public` schema, read and write
    rows and use sequences; default privileges extend that to tables later migrations add."""
    raw = conn.connection.driver_connection

    def run(statement: sql.Composable) -> None:
        conn.exec_driver_sql(statement.as_string(raw))

    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "farmcore.runtime"})
    r = sql.Identifier(role)
    owner = sql.Identifier(conn.execute(text("SELECT current_user")).scalar_one())
    database = sql.Identifier(conn.execute(text("SELECT current_database()")).scalar_one())
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    verb = "ALTER ROLE {} WITH " if exists else "CREATE ROLE {} WITH "
    run(sql.SQL(verb + RUNTIME_ATTRIBUTES + " PASSWORD {}").format(r, sql.Literal(password)))
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
        run(sql.SQL(statement).format(db=database, r=r, owner=owner))
    for table in READ_ONLY_TABLES:
        run(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {} FROM {}").format(sql.Identifier(table), r))


def register(cli: typer.Typer) -> None:
    @cli.command()
    def migrate() -> None:
        """Upgrade the database schema to head and set up the installation (DATABASE_URL)."""
        settings = get_settings()
        command.upgrade(alembic_config(settings.database_url), "head")
        engine = create_engine(settings.database_url)
        try:
            with engine.begin() as conn:
                installation_id = ensure_installation_id(conn)
                if settings.runtime_database_password:
                    ensure_runtime_role(
                        conn,
                        settings.runtime_database_role,
                        settings.runtime_database_password.get_secret_value(),
                    )
        finally:
            engine.dispose()
        typer.echo("migrated to head")
        typer.echo(f"installation {installation_id}")
