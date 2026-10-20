"""`farmcore migrate`: upgrade the database to the latest revision and give the installation
its identity. Run as the database owner (the server's bootstrap superuser); the app and bot
never migrate."""

from __future__ import annotations

import uuid
from importlib.resources import files

import psycopg
import typer
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from farmcore.core.startup import INSTALLATION_SETTING, read_installation_id
from farmcore.settings import Settings, get_settings


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


RUNTIME_ATTRIBUTES = (
    "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
    "CONNECTION LIMIT -1 VALID UNTIL 'infinity'"
)
# PostgreSQL 16+: role memberships carry their grantor (`REVOKE ... GRANTED BY`).
MIN_SERVER_VERSION = 160000


# Taken before revoking PUBLIC's access on every database. GRANT and REVOKE rewrite a
# database's catalog row without a lock of their own, so two such loops at once (restores, a
# migrate) fail with "tuple concurrently updated", as does one whose database is renamed or
# dropped under it. This lock waits for those and holds them off until the transaction ends;
# `pg_database` is shared, so it is one lock for the whole server.
LOCK_DATABASES = "LOCK TABLE pg_catalog.pg_database IN SHARE ROW EXCLUSIVE MODE"


class MigrateError(RuntimeError):
    """A refusal or failure `farmcore migrate` reports in one line (never with SQL text)."""


def check_owner(conn: Connection) -> str:
    """The connected role, which must be a superuser: FarmCore runs its own PostgreSQL server
    (one per installation) and migrates as that server's bootstrap superuser."""
    me, superuser, version = conn.execute(
        text(
            "SELECT current_user, rolsuper, current_setting('server_version_num')::int "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    if version < MIN_SERVER_VERSION:
        raise MigrateError(
            f"FarmCore needs PostgreSQL {MIN_SERVER_VERSION // 10000} or later (the compose file "
            f"runs 17); this server is {version // 10000}."
        )
    if not superuser:
        raise MigrateError(
            "FarmCore expects its own PostgreSQL server; the owner role must be that server's "
            f"bootstrap superuser (POSTGRES_USER, the database owner), not {me}. Use the owner's "
            "DATABASE_URL (POSTGRES_PASSWORD), as the compose `migrate` service does; the app's "
            "runtime role cannot change the schema."
        )
    return me


def _run(conn: Connection, role: str, statements: list[sql.Composable]) -> None:
    """Run through psycopg directly: a failure reports the server's message, never the SQL."""
    raw = conn.connection.driver_connection
    try:
        for statement in statements:
            raw.execute(statement)
    except psycopg.Error as exc:
        detail = exc.diag.message_primary or type(exc).__name__
        raise MigrateError(f"could not set up the runtime role {role}: {detail}") from None


def _rows(conn: Connection, role: str, query, params: dict | None = None) -> list:
    """A catalog query whose failure, like `_run`'s, reports the server's message only."""
    try:
        return conn.execute(query, params or {}).all()
    except DBAPIError as exc:
        diag = getattr(exc.orig, "diag", None)
        detail = getattr(diag, "message_primary", None) or type(exc.orig).__name__
        raise MigrateError(f"could not set up the runtime role {role}: {detail}") from None


def _refuse_owner_as_runtime(conn: Connection, role: str) -> str:
    me = conn.execute(text("SELECT current_user")).scalar_one()
    if role == me:
        raise MigrateError(
            f"RUNTIME_DATABASE_ROLE must differ from the owner role ({me}): the runtime role is "
            "the restricted one."
        )
    return me


def _exists(conn: Connection, role: str) -> bool:
    return conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()


def reset_runtime_role_in(conn: Connection, role: str) -> None:
    """Revoke every privilege granted to an existing runtime role, on this database's objects
    and on shared objects (databases, tablespaces, parameters), whoever granted it.

    `DROP OWNED` would also drop what the role owns, so a role that owns anything is refused
    first; owning nothing, `DROP OWNED` only revokes, default privileges included. It revokes
    as the objects' owners, though, so a grant another role made survives it: those are then
    revoked one by one, as their grantor. If any grant to the role is still left, refuse."""
    _refuse_owner_as_runtime(conn, role)
    if not _exists(conn, role):
        return
    owned = _rows(conn, role, _OWNED, {"r": role})
    if owned:
        where = ", ".join(f"{n} in {db}" for db, n in owned)
        raise MigrateError(
            f"{role} owns database objects ({where}); the runtime role must own nothing. "
            f"Connected to each database named (shared objects: any one), run REASSIGN OWNED "
            f"BY {role} TO <owner>, then migrate again."
        )
    policies = [name for (name,) in _rows(conn, role, _POLICIES, {"r": role})]
    if policies:
        raise MigrateError(
            f"{role} is named in row security policy {', '.join(policies)}; resetting the role "
            "would drop it silently. Remove the role from the policy, then migrate again."
        )
    r = sql.Identifier(role)
    _run(conn, role, [sql.SQL("DROP OWNED BY {}").format(r)])
    statements = []
    for kind, name, column, grantor in _rows(conn, role, _GRANTS_TO, {"r": role}):
        # `name` comes quoted by the server (regclass, quote_ident...): appended, not formatted.
        columns = sql.SQL(" ({})").format(sql.Identifier(column)) if column else sql.SQL("")
        # PostgreSQL revokes an object privilege only as its grantor: become that role.
        statements += [
            sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(grantor)),
            sql.Composed(
                [
                    sql.SQL("REVOKE ALL"),
                    columns,
                    sql.SQL(f" ON {kind} "),
                    sql.SQL(name),
                    sql.SQL(" FROM {} CASCADE").format(r),
                ]
            ),
            sql.SQL("RESET ROLE"),
        ]
    _run(conn, role, statements)
    left = _rows(conn, role, _GRANTS_TO, {"r": role})
    if left:
        raise MigrateError(
            f"{role} still holds {len(left)} privilege(s) after revoking; revoke them by hand "
            "and run migrate again."
        )


# Every privilege granted to a role, here and on shared objects: (object kind, its name as the
# server quotes it, column or NULL, grantor). Default privileges are left to DROP OWNED.
_GRANTS_TO = text(
    """
    WITH acl AS (
        SELECT CASE c.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END AS kind,
               c.oid::regclass::text AS name, NULL::name AS col, a.grantor, a.grantee
          FROM pg_class c, aclexplode(c.relacl) a
        UNION ALL
        SELECT 'TABLE', t.attrelid::regclass::text, t.attname, a.grantor, a.grantee
          FROM pg_attribute t, aclexplode(t.attacl) a
        UNION ALL
        SELECT 'ROUTINE', p.oid::regprocedure::text, NULL, a.grantor, a.grantee
          FROM pg_proc p, aclexplode(p.proacl) a
        UNION ALL
        SELECT 'TYPE', t.oid::regtype::text, NULL, a.grantor, a.grantee
          FROM pg_type t, aclexplode(t.typacl) a
        UNION ALL
        SELECT 'SCHEMA', quote_ident(n.nspname), NULL, a.grantor, a.grantee
          FROM pg_namespace n, aclexplode(n.nspacl) a
        UNION ALL
        SELECT 'LANGUAGE', quote_ident(l.lanname), NULL, a.grantor, a.grantee
          FROM pg_language l, aclexplode(l.lanacl) a
        UNION ALL
        SELECT 'LARGE OBJECT', m.oid::text, NULL, a.grantor, a.grantee
          FROM pg_largeobject_metadata m, aclexplode(m.lomacl) a
        UNION ALL
        SELECT 'FOREIGN DATA WRAPPER', quote_ident(w.fdwname), NULL, a.grantor, a.grantee
          FROM pg_foreign_data_wrapper w, aclexplode(w.fdwacl) a
        UNION ALL
        SELECT 'FOREIGN SERVER', quote_ident(s.srvname), NULL, a.grantor, a.grantee
          FROM pg_foreign_server s, aclexplode(s.srvacl) a
        UNION ALL
        SELECT 'DATABASE', quote_ident(d.datname), NULL, a.grantor, a.grantee
          FROM pg_database d, aclexplode(d.datacl) a
        UNION ALL
        SELECT 'TABLESPACE', quote_ident(s.spcname), NULL, a.grantor, a.grantee
          FROM pg_tablespace s, aclexplode(s.spcacl) a
        UNION ALL
        SELECT 'PARAMETER',
               array_to_string(ARRAY(SELECT quote_ident(part)
                                       FROM unnest(string_to_array(p.parname, '.')) part), '.'),
               NULL, a.grantor, a.grantee
          FROM pg_parameter_acl p, aclexplode(p.paracl) a
    )
    SELECT DISTINCT kind, name, col, pg_get_userbyid(grantor)
      FROM acl WHERE grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)
     ORDER BY 1, 2, 3, 4
    """
)


def keep_connect(conn: Connection, role: str, database_url: str) -> None:
    if _exists(conn, role):
        database = sql.Identifier(make_url(database_url).database)
        _run(
            conn,
            role,
            [sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sql.Identifier(role))],
        )


def ensure_runtime_role(conn: Connection, role: str, password: str) -> None:
    """Create or reset the DML-only runtime role and grant it this database, nothing more.

    The owner (the connected superuser) keeps the schema, the PostGIS extension and every DDL
    right. After this, the runtime role holds exactly: CONNECT on this database; USAGE on the
    `public` schema; SELECT, INSERT, UPDATE, DELETE on its tables and views, except SELECT only
    on `alembic_version` and on the PostGIS extension's own (`spatial_ref_sys`,
    `geometry_columns`, `geography_columns`); USAGE, SELECT on its sequences; the same, by
    default, on tables and sequences the owner creates there later. It is a member of no role,
    has no role settings, and its attributes are reset (no superuser, no create, no
    replication, no RLS bypass, no inheritance, no connection limit, no expiry).

    An existing role is reset first with `reset_runtime_role_in` here (the caller also runs it
    in `postgres`); privileges on objects inside other databases are not reset, but the role
    cannot connect to any other database present at this run (PUBLIC's CONNECT is revoked
    there too). The password is sent as a SCRAM verifier computed here: the plaintext is never
    part of any statement."""
    me = _refuse_owner_as_runtime(conn, role)
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "farmcore.runtime"})
    existed = _exists(conn, role)
    reset_runtime_role_in(conn, role)
    raw = conn.connection.driver_connection
    r, owner = sql.Identifier(role), sql.Identifier(me)
    database = sql.Identifier(conn.execute(text("SELECT current_database()")).scalar_one())
    verifier = raw.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256")
    verb = "ALTER ROLE {} WITH " if existed else "CREATE ROLE {} WITH "
    statements = [
        sql.SQL(verb + RUNTIME_ATTRIBUTES + " PASSWORD {}").format(
            r, sql.Literal(verifier.decode())
        )
    ]
    for granted, grantor in _rows(conn, role, _MEMBERSHIPS, {"r": role}):
        statements.append(
            sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
                sql.Identifier(granted), r, sql.Identifier(grantor)
            )
        )
    for (db,) in _rows(conn, role, _SETTINGS, {"r": role}):
        if db is None:
            statements.append(sql.SQL("ALTER ROLE {} RESET ALL").format(r))
        else:
            statements.append(
                sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(r, sql.Identifier(db))
            )
    _run(conn, role, [sql.SQL(LOCK_DATABASES)])
    statements += [
        sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(d))
        for (d,) in _rows(conn, role, _OTHER_DBS)
    ]
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
    for (table,) in [("alembic_version",), *_rows(conn, role, _EXTENSION_RELATIONS)]:
        statements.append(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {} FROM {}, PUBLIC").format(
                sql.Identifier(table), r
            )
        )
    _run(conn, role, statements)
    left = conn.execute(_MEMBER_COUNT, {"r": role}).scalar_one()
    if left:
        raise MigrateError(
            f"{role} is still a member of {left} role(s) after revoking; remove those "
            "memberships by hand and run migrate again."
        )


# Objects a role owns anywhere on the server: (database, count); `-` = shared objects.
_OWNED = text(
    "SELECT coalesce(d.datname, '-'), count(*) FROM pg_shdepend s "
    "LEFT JOIN pg_database d ON d.oid = s.dbid WHERE s.deptype = 'o' "
    "AND s.refclassid = 'pg_authid'::regclass "
    "AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = :r) GROUP BY 1 ORDER BY 1"
)
# Every membership of a role, with who granted it (a REVOKE removes only one grantor's grant).
_MEMBERSHIPS = text(
    "SELECT g.rolname, gr.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
    "JOIN pg_roles gr ON gr.oid = m.grantor "
    "WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = :r)"
)
_MEMBER_COUNT = text(
    "SELECT count(*) FROM pg_auth_members "
    "WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :r)"
)
# Role-level settings: NULL = for every database.
_SETTINGS = text(
    "SELECT d.datname FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid = s.setdatabase "
    "WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = :r)"
)
# Every database but this one and template0 (no connections, kept pristine). PUBLIC may
# connect to a new database and create temporary tables there by default; runtime must not.
_OTHER_DBS = text(
    "SELECT datname FROM pg_database WHERE datname NOT IN (current_database(), 'template0') "
    "ORDER BY 1"
)
# Row security policies naming the role (DROP OWNED would remove the role from them).
_POLICIES = text(
    "SELECT polname FROM pg_policy "
    "WHERE (SELECT oid FROM pg_roles WHERE rolname = :r) = ANY(polroles) ORDER BY 1"
)
# Tables and views in `public` that belong to an extension (PostGIS): read-only for runtime.
_EXTENSION_RELATIONS = text(
    "SELECT c.relname FROM pg_depend d JOIN pg_class c ON c.oid = d.objid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE d.classid = 'pg_class'::regclass "
    "AND d.deptype = 'e' AND n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p') "
    "ORDER BY 1"
)


def migrate_installation(settings: Settings) -> str:
    """Upgrade to head, give the installation its id, set up the runtime role; the id.

    Raises `MigrateError` on a refusal."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            check_owner(conn)
        command.upgrade(alembic_config(settings.database_url), "head")
        if settings.runtime_database_password:
            # Objects in `postgres` and shared objects first, in their own transaction.
            maintenance = create_engine(make_url(settings.database_url).set(database="postgres"))
            try:
                with maintenance.begin() as conn:
                    reset_runtime_role_in(conn, settings.runtime_database_role)
                    # The reset revoked CONNECT on the installation database; give it back
                    # now, so a failure in the next step never locks out the running app.
                    keep_connect(conn, settings.runtime_database_role, settings.database_url)
            finally:
                maintenance.dispose()
        with engine.begin() as conn:
            installation_id = ensure_installation_id(conn)
            if settings.runtime_database_password:
                ensure_runtime_role(
                    conn,
                    settings.runtime_database_role,
                    settings.runtime_database_password.get_secret_value(),
                )
        return installation_id
    finally:
        engine.dispose()


def register(cli: typer.Typer) -> None:
    @cli.command()
    def migrate() -> None:
        """Upgrade the database schema to head and set up the installation (DATABASE_URL).

        Run as this server's bootstrap superuser (the database owner); anything else is
        refused."""
        try:
            installation_id = migrate_installation(get_settings())
        except MigrateError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        typer.echo("migrated to head")
        typer.echo(f"installation {installation_id}")
