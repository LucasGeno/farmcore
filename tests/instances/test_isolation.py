"""Installations cannot see or change each other, and the runtime role cannot change the schema.

Database-level tests run against a throwaway PostGIS server (conftest.py), as the real roles;
tests that only probe an installation share one per module. The cookie tests run the app over
HTTP. The Compose tests at the bottom build the image once and run two real projects.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg.errors
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.instances.conftest import REPO, Project, docker_available, inspect, postgis_image
from tests.instances.support import DatabaseList, run_cli, says, wait_for


@dataclass(frozen=True)
class Installed:
    database: str
    owner_url: str
    runtime_url: str
    role: str
    password: str

    @property
    def env(self) -> dict[str, str]:
        return {
            "DATABASE_URL": self.owner_url,
            "RUNTIME_DATABASE_ROLE": self.role,
            "RUNTIME_DATABASE_PASSWORD": self.password,
        }


class Installs:
    """`farmcore migrate` new databases, each with its own runtime role; dropped at the end."""

    def __init__(self, server, template: str) -> None:
        self.server = server
        self.template = template  # migrated once; a copy only needs the runtime role set up
        self.admin = create_engine(server.url("farmcore"), isolation_level="AUTOCOMMIT")
        self.made: list[Installed] = []
        self.roles: list[str] = []  # other roles a test made, dropped after the databases

    def __call__(self, password: str | None = None, before=None, migrate: bool = True):
        """`before(admin_connection, role)` runs before `farmcore migrate`."""
        name = f"inst_{uuid.uuid4().hex[:10]}"
        role = f"rt_{uuid.uuid4().hex[:10]}"
        password = password or secrets.token_hex(16)
        copy = f' TEMPLATE "{self.template}"' if migrate else ""
        with self.admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"{copy}'))
            if before is not None:
                before(conn, role)
        if not migrate:
            engine = create_engine(self.server.url(name))
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION postgis"))
            engine.dispose()
        inst = Installed(
            name, self.server.url(name), self.server.url(name, role, password), role, password
        )
        self.made.append(inst)
        if migrate:
            result = run_cli("migrate", env=inst.env)
            assert result.exit_code == 0, result.output
        return inst

    def close(self) -> None:
        with self.admin.connect() as conn:
            for inst in self.made:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{inst.database}" WITH (FORCE)'))
            for inst in self.made:
                exists = text("SELECT 1 FROM pg_roles WHERE rolname = :r")
                if conn.execute(exists, {"r": inst.role}).scalar() is None:
                    continue
                conn.execute(text(f'DROP OWNED BY "{inst.role}"'))
                conn.execute(text(f'DROP ROLE IF EXISTS "{inst.role}"'))
            for role in self.roles:
                conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        self.admin.dispose()


@pytest.fixture
def install(pg_server, templates) -> Iterator[Installs]:
    made = Installs(pg_server, templates.migrated)
    yield made
    made.close()


@pytest.fixture(scope="module")
def installs(pg_server, templates) -> Iterator[Installs]:
    made = Installs(pg_server, templates.migrated)
    yield made
    made.close()


@pytest.fixture(scope="module")
def installed(installs) -> Installed:
    """One migrated installation that the module's tests only probe."""
    return installs()


@pytest.fixture(scope="module")
def other_installed(installs) -> Installed:
    """A second installation on the same server."""
    return installs()


def _denied(url: str, sql: str) -> None:
    # Autocommit: CREATE DATABASE refuses a transaction block before it checks privileges.
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect() as conn:
            conn.execute(text(sql))
        assert isinstance(exc.value.orig, psycopg.errors.InsufficientPrivilege), sql
    finally:
        engine.dispose()


def _scalar(url: str, sql: str, **params):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


def _health_id(url: str) -> str:
    from fastapi.testclient import TestClient

    from farmcore.main import create_app
    from farmcore.settings import Settings

    with TestClient(create_app(Settings(_env_file=None, database_url=url), manifests=[])) as c:
        return c.get("/health").json()["installation_id"]


# --- A: the installation id and the runtime role ---------------------------------------------


def test_migrate_gives_one_random_id_and_a_rerun_keeps_it_and_the_runtime_password(install):
    """An upgrade or `compose up` must never re-key the installation (every cookie invalid) or
    lock the app out with a new password."""
    inst = install()
    first = _health_id(inst.owner_url)
    assert uuid.UUID(first).version == 4

    again = run_cli("migrate", env=inst.env)

    assert again.exit_code == 0, again.output
    assert _health_id(inst.owner_url) == first
    assert _scalar(inst.runtime_url, "SELECT count(*) FROM farm") == 0  # same password


def test_a_session_override_of_the_setting_does_not_change_the_reported_id(installed):
    """Someone with a connection string must not spoof another installation's id, and with it
    its cookie scope."""
    from farmcore.core.startup import read_installation_id

    real = _health_id(installed.runtime_url)
    forged = str(uuid.uuid4())
    engine = create_engine(
        f"{installed.runtime_url}?options=-c%20farmcore.installation_id%3D{forged}"
    )
    try:
        with engine.connect() as conn:  # the override is live in this session
            assert conn.execute(text("SHOW farmcore.installation_id")).scalar_one() == forged
            assert read_installation_id(conn) == real
    finally:
        engine.dispose()


def test_runtime_role_reads_and_writes_rows_also_in_tables_a_later_migration_adds(install):
    inst = install()
    with create_engine(inst.owner_url).begin() as conn:
        conn.execute(text("CREATE TABLE later_table (id bigserial PRIMARY KEY, v text)"))
    engine = create_engine(inst.runtime_url)
    try:
        with engine.begin() as conn:
            farm_id = conn.execute(
                text("INSERT INTO farm (name) VALUES ('Runtime farm') RETURNING id")
            ).scalar_one()
            conn.execute(text("UPDATE farm SET name = 'Renamed' WHERE id = :i"), {"i": farm_id})
            conn.execute(text("DELETE FROM farm WHERE id = :i"), {"i": farm_id})
            conn.execute(text("INSERT INTO later_table (v) VALUES ('x')"))
            assert conn.execute(text("SELECT v FROM later_table")).scalar_one() == "x"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE DATABASE runtime_made",
        "CREATE ROLE runtime_made",
        "CREATE EXTENSION pgcrypto",  # a trusted extension: the database's CREATE right
        "CREATE TABLE public.runtime_made (id int)",
        "CREATE TEMP TABLE runtime_made (id int)",
        "TRUNCATE farm",
        "UPDATE alembic_version SET version_num = 'runtime_made'",
        "INSERT INTO spatial_ref_sys (srid) VALUES (999999)",
        "ALTER DATABASE {db} SET farmcore.installation_id = '00000000-0000-4000-8000-000000000000'",
    ],
)
def test_the_runtime_role_is_refused_every_right_it_was_never_given(installed, sql) -> None:
    """The DML-only role must not regain DDL or cluster rights, or change its own identity."""
    _denied(installed.runtime_url, sql.format(db=f'"{installed.database}"'))


@pytest.mark.parametrize("database", ["another installation's", "one made before this migrate"])
def test_the_runtime_role_cannot_open_another_database_on_the_server(
    install, installed, other_installed, pg_server, request, database
) -> None:
    """A's credentials must not read B's database on a shared server, nor any database that
    existed when A's migrate ran (PUBLIC may CONNECT to a new database by default)."""
    probe, name = installed, other_installed.database
    if database == "one made before this migrate":
        name = f"bystander_{uuid.uuid4().hex[:8]}"

        def drop() -> None:
            with install.admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

        request.addfinalizer(drop)
        with install.admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))  # default ACL: PUBLIC CONNECT
        probe = install()  # this migrate must revoke PUBLIC's CONNECT there
    engine = create_engine(pg_server.url(name, probe.role, probe.password))
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect():
            pass
        assert "permission denied for database" in str(exc.value)
    finally:
        engine.dispose()


def test_an_existing_runtime_role_loses_every_membership_and_its_role_settings(
    install, pg_server
) -> None:
    """A pre-existing role granted the owner could otherwise SET ROLE to it and run DDL;
    PostgreSQL 16+ removes with a plain REVOKE only the revoker's own grants."""
    granter = f"granter_{uuid.uuid4().hex[:8]}"

    def pre_create(conn, role: str) -> None:
        conn.execute(text(f'CREATE ROLE "{role}" LOGIN'))
        conn.execute(text(f'GRANT farmcore, pg_read_all_data TO "{role}"'))
        conn.execute(text(f'ALTER ROLE "{role}" SET search_path = pg_catalog'))
        conn.execute(text(f'CREATE ROLE "{granter}"'))
        conn.execute(text(f'GRANT pg_monitor TO "{granter}" WITH ADMIN OPTION'))
        conn.execute(text(f'GRANT pg_monitor TO "{role}" GRANTED BY "{granter}"'))

    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    try:
        inst = install(before=pre_create)
        _denied(inst.runtime_url, "SET ROLE farmcore")
        assert _scalar(inst.runtime_url, "SHOW search_path") != "pg_catalog"
        members = (
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = :r"
        )
        assert _scalar(pg_server.url("farmcore"), members, r=inst.role) == 0
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP ROLE IF EXISTS "{granter}"'))
        admin.dispose()


ATTACK = """
CREATE ROLE {g};
GRANT UPDATE ON alembic_version TO {g} WITH GRANT OPTION;
GRANT CREATE ON SCHEMA public TO {g};
SET ROLE {g};
GRANT UPDATE ON alembic_version TO {r};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {r};
ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {r};
RESET ROLE;
ALTER DEFAULT PRIVILEGES FOR ROLE farmcore IN SCHEMA public GRANT ALL ON TABLES TO {r};
ALTER DEFAULT PRIVILEGES FOR ROLE farmcore GRANT ALL ON TABLES TO {r};
ALTER DEFAULT PRIVILEGES FOR ROLE farmcore GRANT EXECUTE ON ROUTINES TO {r};
GRANT CREATE ON DATABASE {db} TO {r};
GRANT ALL ON SCHEMA public TO {r};
GRANT TRUNCATE, REFERENCES, TRIGGER ON farm TO {r};
CREATE PROCEDURE attack_proc() LANGUAGE sql AS 'SELECT 1';
REVOKE EXECUTE ON PROCEDURE attack_proc() FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE attack_proc() TO {r};
CREATE TYPE attack_type AS ENUM ('a');
REVOKE USAGE ON TYPE attack_type FROM PUBLIC;
GRANT USAGE ON TYPE attack_type TO {r};
GRANT SET ON PARAMETER work_mem TO {r};
GRANT SET ON PARAMETER foo."select" TO {g} WITH GRANT OPTION;
SET ROLE {g};
GRANT SET ON PARAMETER foo."select" TO {r};
RESET ROLE;
SELECT lo_create(424242);
GRANT SELECT, UPDATE ON LARGE OBJECT 424242 TO {r};
GRANT UPDATE, DELETE ON alembic_version TO PUBLIC;
GRANT INSERT, TRUNCATE ON spatial_ref_sys, geometry_columns TO PUBLIC;
ALTER ROLE {r} CONNECTION LIMIT 7 VALID UNTIL '2099-01-01';
ALTER ROLE {r} SET statement_timeout = '1s';
ALTER ROLE {r} IN DATABASE {db} SET work_mem = '64MB';
"""

# Each probe, run as the superuser, must be false (or the stated value) after migrate.
PROBES = {
    "UPDATE alembic_version": "has_table_privilege({r!r}, 'alembic_version', 'UPDATE')",
    "DELETE alembic_version": "has_table_privilege({r!r}, 'alembic_version', 'DELETE')",
    "TRUNCATE farm": "has_table_privilege({r!r}, 'farm', 'TRUNCATE')",
    "TRIGGER farm": "has_table_privilege({r!r}, 'farm', 'TRIGGER')",
    "INSERT spatial_ref_sys": "has_table_privilege({r!r}, 'spatial_ref_sys', 'INSERT')",
    "TRUNCATE geometry_columns": "has_table_privilege({r!r}, 'geometry_columns', 'TRUNCATE')",
    "CREATE on schema": "has_schema_privilege({r!r}, 'public', 'CREATE')",
    "CREATE on database": "has_database_privilege({r!r}, current_database(), 'CREATE')",
    "EXECUTE procedure": "has_function_privilege({r!r}, 'attack_proc()', 'EXECUTE')",
    "USAGE type": "has_type_privilege({r!r}, 'attack_type', 'USAGE')",
    "SET work_mem (ACL)": (
        "EXISTS (SELECT 1 FROM pg_parameter_acl p, aclexplode(p.paracl) a "
        "WHERE p.parname = 'work_mem' AND a.grantee = {r!r}::regrole)"
    ),
    "SET foo.select (a name needing quotes)": (
        "EXISTS (SELECT 1 FROM pg_parameter_acl p, aclexplode(p.paracl) a "
        "WHERE p.parname = 'foo.select' AND a.grantee = {r!r}::regrole)"
    ),
    "large object": (
        "EXISTS (SELECT 1 FROM pg_largeobject_metadata m, aclexplode(m.lomacl) a "
        "WHERE m.oid = 424242 AND a.grantee = {r!r}::regrole)"
    ),
    "default privileges beyond the intended": (
        "EXISTS (SELECT 1 FROM pg_default_acl d, aclexplode(d.defaclacl) a "
        "WHERE a.grantee = {r!r}::regrole AND NOT (d.defaclrole = 'farmcore'::regrole "
        "AND d.defaclnamespace = 'public'::regnamespace "
        "AND a.privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'USAGE')))"
    ),
    "role settings": ("EXISTS (SELECT 1 FROM pg_db_role_setting WHERE setrole = {r!r}::regrole)"),
    "connection limit": "(SELECT rolconnlimit <> -1 FROM pg_roles WHERE rolname = {r!r})",
    "expiry": (
        "(SELECT rolvaliduntil IS DISTINCT FROM 'infinity' FROM pg_roles WHERE rolname = {r!r})"
    ),
}


def test_an_existing_runtime_role_is_reset_to_exactly_the_intended_rights(
    install, pg_server
) -> None:
    """Grants somebody else made survive DROP OWNED: the reset revokes them as their grantor,
    on every kind of object, here and in `postgres`, and does so again on every migrate."""
    inst = install()
    granter = f"g_{uuid.uuid4().hex[:8]}"
    owner = create_engine(inst.owner_url, isolation_level="AUTOCOMMIT")
    postgres = create_engine(pg_server.url("postgres"), isolation_level="AUTOCOMMIT")
    in_postgres = f"attack_{inst.role}"
    try:
        with owner.connect() as conn:
            for statement in ATTACK.format(g=granter, r=inst.role, db=inst.database).split(";"):
                if statement.strip():
                    conn.execute(text(statement))
        with postgres.connect() as conn:  # an object in the maintenance database
            conn.execute(text(f"CREATE TABLE {in_postgres} (id int)"))
            conn.execute(text(f"GRANT SELECT ON {in_postgres} TO {inst.role}"))
        for _ in range(2):  # twice in a row: idempotent
            result = run_cli("migrate", env=inst.env)
            assert result.exit_code == 0, result.output
        with postgres.connect() as conn:
            privilege = f"SELECT has_table_privilege('{inst.role}', '{in_postgres}', 'SELECT')"
            assert conn.execute(text(privilege)).scalar_one() is False
        with owner.connect() as conn:
            held = {
                name: conn.execute(text("SELECT " + probe.format(r=inst.role))).scalar_one()
                for name, probe in PROBES.items()
            }
            # A table the owner creates afterwards gets only the intended defaults.
            conn.execute(text("CREATE TABLE later_owner_table (id int)"))
            held["TRUNCATE later table"] = conn.execute(
                text(f"SELECT has_table_privilege('{inst.role}', 'later_owner_table', 'TRUNCATE')")
            ).scalar_one()
            assert conn.execute(
                text(f"SELECT has_table_privilege('{inst.role}', 'later_owner_table', 'INSERT')")
            ).scalar_one()
        assert {name for name, value in held.items() if value} == set()
        _denied(inst.runtime_url, "UPDATE alembic_version SET version_num = version_num")
        _denied(inst.runtime_url, "CALL attack_proc()")
    finally:
        with owner.connect() as conn:
            conn.execute(text(f'DROP OWNED BY "{granter}"'))
            conn.execute(text(f"REVOKE SET ON PARAMETER work_mem FROM {inst.role}"))
            conn.execute(text(f'REVOKE SET ON PARAMETER foo."select" FROM {granter} CASCADE'))
        owner.dispose()
        with postgres.connect() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {in_postgres}"))
        postgres.dispose()
        admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP OWNED BY "{granter}"'))
            conn.execute(text(f'DROP ROLE "{granter}"'))
        admin.dispose()


def _not_the_superuser(install, pg_server, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    owner, database, password = f"own_{suffix}", f"shared_{suffix}", secrets.token_hex(16)
    with install.admin.connect() as conn:
        conn.execute(text(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{password}'"))
        conn.execute(text(f"CREATE DATABASE {database} OWNER {owner}"))
    inst = Installed(database, pg_server.url(database, owner, password), "", f"rt_{suffix}", "x")
    install.made.append(inst)
    install.roles.append(owner)

    def nothing_changed() -> None:
        roles = "SELECT count(*) FROM pg_roles WHERE rolname = :r"
        assert _scalar(pg_server.url("farmcore"), roles, r=inst.role) == 0
        assert _scalar(pg_server.url(database), "SELECT to_regclass('alembic_version')") is None
        with install.admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {database} WITH (FORCE)"))

    return {**inst.env, "DATABASE_URL": inst.owner_url}, "superuser", nothing_changed


def _named_like_the_owner(install, pg_server, monkeypatch):
    inst = install()
    return {**inst.env, "RUNTIME_DATABASE_ROLE": "farmcore"}, "must differ from the owner", None


def _owning_objects(install, pg_server, monkeypatch):
    inst = install()
    with create_engine(inst.owner_url).begin() as conn:
        conn.execute(text("CREATE TABLE owned_by_runtime (id int)"))
        conn.execute(text(f'ALTER TABLE owned_by_runtime OWNER TO "{inst.role}"'))
    return inst.env, "must own nothing", None


def _in_a_row_security_policy(install, pg_server, monkeypatch):
    """DROP OWNED would silently drop the policy; this refusal comes in the second step, after
    the `postgres` pass committed: the running app must keep connecting."""
    inst = install()
    with create_engine(inst.owner_url).begin() as conn:
        conn.execute(text(f'CREATE POLICY runtime_policy ON farm TO "{inst.role}" USING (true)'))

    def still_connects() -> None:
        assert _scalar(inst.runtime_url, "SELECT count(*) FROM farm") == 0

    return inst.env, "row security policy", still_connects


def _too_old_a_server(install, pg_server, monkeypatch):
    from farmcore.core.commands import migrate

    inst = install()
    monkeypatch.setattr(migrate, "MIN_SERVER_VERSION", 990000)
    return inst.env, "PostgreSQL 99 or later", None


@pytest.mark.parametrize(
    "case",
    [_not_the_superuser, _named_like_the_owner, _owning_objects, _in_a_row_security_policy,
     _too_old_a_server],
    ids=["owner not the superuser", "runtime named like the owner", "runtime owns objects",
         "runtime in a row security policy", "PostgreSQL too old"],
)  # fmt: skip
def test_migrate_refuses_a_server_or_role_it_cannot_make_safe_and_says_why(
    install, pg_server, monkeypatch, case
) -> None:
    env, phrase, then = case(install, pg_server, monkeypatch)

    result = run_cli("migrate", env=env)

    assert result.exit_code == 1, result.output
    assert phrase in result.output and "Traceback" not in result.output
    if then:
        then()


HOSTILE_PASSWORD = "p'\"%s%%{}$$\\;--\n" + secrets.token_hex(8)


def test_a_runtime_password_with_sql_and_format_characters_works_and_is_never_echoed(
    install, pg_server
) -> None:
    """The password must never reach the terminal or the server log, and must survive quoting,
    even when a role statement fails."""
    inst = install(password=HOSTILE_PASSWORD, migrate=False)

    result = run_cli("migrate", env=inst.env)
    failed = run_cli("migrate", env={**inst.env, "RUNTIME_DATABASE_ROLE": "pg_monitor"})

    assert result.exit_code == 0, result.output
    assert _scalar(inst.runtime_url, "SELECT current_user") == inst.role
    stored = "SELECT rolpassword FROM pg_authid WHERE rolname = :r"
    assert _scalar(pg_server.url("farmcore"), stored, r=inst.role).startswith("SCRAM-SHA-256$")
    assert failed.exit_code == 1 and "runtime role" in failed.output  # reserved: ALTER fails
    log = pg_server.log()
    assert inst.role in log  # the role statements are logged ...
    for shown in (result.output, failed.output, log):
        assert HOSTILE_PASSWORD not in shown  # ... but never with the password
        assert HOSTILE_PASSWORD.split("\n")[1] not in shown
    assert "PASSWORD" not in failed.output and "SCRAM" not in failed.output


def test_a_migrate_kept_from_the_database_list_says_so_and_a_rerun_succeeds(
    install, fast_lock_timeout
) -> None:
    """Another restore or migrate holds `pg_database`: migrate gives up after the lock timeout
    instead of hanging past compose's grace, and says to run it again."""
    inst = install()
    from tests.instances.support import Databases

    admin = Databases(install.server)
    held = DatabaseList(admin)
    held.lock()
    try:
        result = run_cli("migrate", env=inst.env)
    finally:
        held.close()
        admin.close()

    assert result.exit_code == 1, result.output
    says(result.output, "busy")
    assert "run migrate again" in result.output
    again = run_cli("migrate", env=inst.env)
    assert again.exit_code == 0, again.output


@pytest.mark.parametrize("lock", ["installation id", "runtime role on a first migrate"])
def test_a_migrate_queued_on_its_own_advisory_locks_waits_past_the_lock_timeout(
    install, fast_lock_timeout, lock
) -> None:
    """Only `pg_database` waits time out: a migrate behind another one on this installation's
    own locks must wait as long as it takes (iso 03 review: runtime lock under lock_timeout)."""
    if lock == "installation id":
        inst, key = install(), "farmcore.installation_id"
    else:
        inst, key = install(migrate=False), "farmcore.runtime"
    holder = create_engine(inst.owner_url)
    conn = holder.connect()
    conn.begin()
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    waiting = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
    )

    def release() -> None:
        try:
            wait_for(lambda: _scalar(inst.owner_url, waiting) == 1, "migrate to queue")
            time.sleep(3 * float(fast_lock_timeout.removesuffix("ms")) / 1000)
        finally:
            conn.rollback()
            conn.close()

    releaser = threading.Thread(target=release, daemon=True)
    releaser.start()
    try:
        result = run_cli("migrate", env=inst.env)
    finally:
        releaser.join()
        holder.dispose()

    assert result.exit_code == 0, result.output


# --- B: cookies and identity over HTTP ----------------------------------------------------------

BASE = "https://testserver"


def _app(url: str, token: str, **kwargs):
    from pydantic import SecretStr

    from farmcore.main import create_app
    from farmcore.settings import Settings

    settings = Settings(_env_file=None, database_url=url, admin_token=SecretStr(token), **kwargs)
    return create_app(settings, app_packages=())


def _signed_in(client) -> bool:
    return "You are signed in as admin." in client.get("/admin/sign-in").text


def _sign_in(client, token: str):
    resp = client.post(
        "/admin/sign-in", data={"token": token}, headers={"Origin": BASE}, follow_redirects=False
    )
    assert resp.status_code == 303
    return resp


def _share(src, dst) -> None:
    """One browser, one host: every cookie either installation set is sent to both."""
    for cookie in src.cookies.jar:
        dst.cookies.jar.set_cookie(cookie)


def test_one_browser_signs_in_to_each_installation_on_a_host_only_with_its_own_cookie(
    installed, other_installed
) -> None:
    """Admin and member sessions must never cross installations (AC iso 01 #1)."""
    from fastapi.testclient import TestClient

    token = secrets.token_hex(24)  # the operator reused one admin token for both
    with (
        TestClient(_app(installed.runtime_url, token), base_url=BASE) as ca,
        TestClient(_app(other_installed.runtime_url, token), base_url=BASE) as cb,
    ):
        _sign_in(ca, token)
        _share(ca, cb)
        assert _signed_in(ca) and not _signed_in(cb)
        _sign_in(cb, token)
        _share(cb, ca)
        assert _signed_in(ca) and _signed_in(cb)  # signing in to B kept A
        assert len({c.name for c in ca.cookies.jar}) == 2


def test_only_this_installations_cookies_reach_the_app_and_every_cookie_it_sets_is_scoped():
    """A foreign or legacy cookie must not reach the app under a plain name."""
    from farmcore.core.cookie_scope import InstallationCookies

    installation = "3f1c2b7a-9d4e-4c1a-8b2f-0e5d6c7b8a91"
    own = "fc-3f1c2b7a9d4e-"  # the documented scheme: the id's first 12 hex digits
    seen: dict = {}

    async def app(scope, receive, send) -> None:
        seen["cookie"] = dict(scope["headers"]).get(b"cookie", b"").decode()
        seen["prefix"] = scope["state"]["cookie_prefix"]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"set-cookie", b"session=x; HttpOnly"), (b"x-other", b"y")],
            }
        )
        await send({"type": "http.response.body", "body": b""})  # fmt: skip

    sent: list = []

    async def send(message) -> None:
        sent.append(message)

    header = (
        f"{own}farmcore_notifications_read_1=abc; farmcore_notifications_read_2=plain-name; "
        "farmcore_session=foreign-or-legacy; fc-000000000000-farmcore_session=other"
    )
    scope = {"type": "http", "headers": [(b"cookie", header.encode())]}
    asyncio.run(InstallationCookies(app, installation)(scope, None, send))

    assert seen == {"cookie": "farmcore_notifications_read_1=abc", "prefix": own}
    assert sent[0]["headers"] == [(b"set-cookie", f"{own}session=x; HttpOnly".encode()),
                                  (b"x-other", b"y")]  # fmt: skip


def test_the_notification_read_cookie_is_scoped_per_installation(installed) -> None:
    """bell.js writes the read-state cookie itself, under the name the page gives it."""
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from farmcore.main import create_app
    from farmcore.settings import Settings

    created = run_cli("farms", "create", "F", env={"DATABASE_URL": installed.runtime_url})
    farm_id = created.output.split()[1]
    token = secrets.token_hex(24)
    settings = Settings(
        _env_file=None, database_url=installed.runtime_url, admin_token=SecretStr(token)
    )
    with TestClient(create_app(settings), base_url=BASE, headers={"X-Admin-Token": token}) as c:
        installation = c.get("/health").json()["installation_id"]
        page = c.get(f"/notifications?farm={farm_id}").text
    name = page.split('data-cookie="')[1].split('"')[0]
    assert name == f"fc-{uuid.UUID(installation).hex[:12]}-farmcore_notifications_read_{farm_id}"


def test_a_public_app_without_an_installation_id_still_scopes_its_cookies(install, caplog) -> None:
    """Fail closed before `migrate` ran on a network deployment: a per-process scope (a
    restart signs everyone out rather than sharing a cookie), and a warning."""
    from alembic import command

    from farmcore.core.commands.migrate import alembic_config

    inst = install(migrate=False)
    command.upgrade(alembic_config(inst.owner_url), "head")  # schema, but no installation id
    token = secrets.token_hex(24)
    names = []
    from fastapi.testclient import TestClient

    with caplog.at_level("WARNING", logger="farmcore.main"):
        for _ in range(2):
            app = _app(inst.owner_url, token, farmcore_public=True)
            with TestClient(app, base_url=BASE) as c:
                resp = _sign_in(c, token)
            names += [h.split("=", 1)[0] for h in resp.headers.get_list("set-cookie")]
    assert "farmcore migrate" in caplog.text
    assert all(n.startswith("fc-") and n.endswith("-admin_token") for n in names)
    assert names[0] != names[1]


# --- L: two real Compose projects from one image -------------------------------------------


@pytest.fixture(scope="module")
def two_installations(new_project) -> tuple[Project, Project]:
    """Projects A and B from one image, each with its own name, env file and secrets."""
    a, b = new_project(), new_project()
    a.up()
    b.up()
    return a, b


@pytest.mark.slow
def test_two_projects_run_side_by_side_with_their_own_identity_and_share_nothing(
    two_installations,
) -> None:
    """No name, network, volume, bind mount or anonymous volume in common (AC iso 01 #1)."""
    a, b = two_installations
    ha, hb = a.health(), b.health()
    assert ha["db"] == hb["db"] == "ok"
    assert uuid.UUID(ha["installation_id"]) != uuid.UUID(hb["installation_id"])

    def resources(p: Project) -> tuple[set[str], set[str]]:
        networks, mounts = set(), set()
        for cid in p.compose("ps", "-a", "-q").stdout.split():
            info = inspect(cid)
            networks |= set(info["NetworkSettings"]["Networks"])
            mounts |= {m.get("Name") or m["Source"] for m in info["Mounts"]}
            for mount in info["Mounts"]:  # `down` without `-v` leaves only named volumes
                if mount["Type"] == "volume":
                    assert mount["Name"].startswith(f"{p.name}_"), (info["Name"], mount)
        return networks, mounts

    (na, ma), (nb, mb) = resources(a), resources(b)
    assert na and nb and not na & nb
    assert ma and mb and not ma & mb
    for info in map(inspect, (a.container("app"), b.container("app"))):
        assert all(m["Type"] == "volume" for m in info["Mounts"]), info["Mounts"]


def _psql(project: Project, user: str, password: str, sql: str, host: str = "db") -> str:
    """psql from a throwaway container on `project`'s network (password auth over TCP)."""
    network = next(iter(inspect(project.container("db"))["NetworkSettings"]["Networks"]))
    url = f"postgresql://{user}:{password}@{host}:5432/farmcore"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", network, "--entrypoint", "psql",
            postgis_image(), url, "-v", "ON_ERROR_STOP=1", "-tAc", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    return result.stdout + result.stderr


@pytest.mark.slow
def test_app_and_bot_hold_only_runtime_credentials_and_never_migrate(two_installations) -> None:
    a, _ = two_installations
    config = json.loads(a.compose("--profile", "telegram", "config", "--format", "json").stdout)
    services = config["services"]
    for name in ("app", "bot"):
        service = services[name]
        env = service["environment"]
        assert env["DATABASE_URL"].startswith("postgresql+psycopg://farmcore_app:")
        assert a.env["POSTGRES_PASSWORD"] not in json.dumps(service)
        assert service["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        assert "migrate" not in " ".join(service.get("command") or [])
    assert services["migrate"]["command"] == ["farmcore", "migrate"]
    image_cmd = inspect(a.container("app"))["Config"]["Cmd"]
    assert "migrate" not in " ".join(image_cmd)
    migrate = inspect(a.container("migrate"))["State"]
    assert migrate["Status"] == "exited" and migrate["ExitCode"] == 0
    out = _psql(a, "farmcore_app", a.env["RUNTIME_DATABASE_PASSWORD"], "CREATE TABLE x (id int)")
    assert "permission denied" in out, out


@pytest.mark.slow
def test_runtime_credentials_of_one_project_cannot_open_the_others_database(
    two_installations,
) -> None:
    a, b = two_installations
    own = _psql(b, "farmcore_app", b.env["RUNTIME_DATABASE_PASSWORD"], "SELECT count(*) FROM farm")
    assert own.strip() == "0"  # a fresh installation has no farms: demo data is opt-in
    foreign = _psql(b, "farmcore_app", a.env["RUNTIME_DATABASE_PASSWORD"], "SELECT 1")
    assert "password authentication failed" in foreign
    # A's app cannot even resolve B's database container: the networks are separate.
    b_db = inspect(b.container("db"))["Name"].lstrip("/")
    probe = "import socket,sys; socket.gethostbyname(sys.argv[1])"
    resolved = subprocess.run(
        ["docker", "exec", a.container("app"), "python", "-c", probe, b_db],
        capture_output=True, text=True,
    )  # fmt: skip
    assert resolved.returncode != 0


@pytest.mark.slow
def test_stopping_one_project_leaves_the_other_healthy(two_installations) -> None:
    a, b = two_installations
    before = b.health()["installation_id"]
    a.compose("stop")
    try:
        assert b.health()["installation_id"] == before  # 503 raises
        state = inspect(b.container("app"))["State"]
        assert state["Running"] and state["Health"]["Status"] == "healthy"
    finally:
        a.compose("start")


@pytest.mark.skipif(not docker_available(), reason="needs Docker")
def test_without_farmcore_image_each_project_builds_its_own_tag(tmp_path) -> None:
    env = tmp_path / "x.env"
    env.write_text("POSTGRES_PASSWORD=x\nRUNTIME_DATABASE_PASSWORD=y\nADMIN_TOKEN=z\n")
    images = set()
    for project in ("farm-a", "farm-b"):
        out = subprocess.run(
            ["docker", "compose", "-p", project, "--env-file", str(env), "--profile", "telegram",
             "config", "--format", "json"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        services = json.loads(out)["services"]
        tags = {services[s]["image"] for s in ("migrate", "app", "bot")}
        assert tags == {f"farmcore-{project}:local"}
        images |= tags
    assert len(images) == 2
