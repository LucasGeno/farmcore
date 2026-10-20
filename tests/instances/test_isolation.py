"""Installations cannot see or change each other, and the runtime role cannot change the schema.

Database-level tests run against a throwaway PostGIS server (conftest.py). The Compose tests at
the bottom build the image once and run two real Compose projects side by side.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg.errors
import pytest
from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from farmcore.settings import get_settings
from tests.instances.conftest import REPO, Project, docker_available, inspect, postgis_image


@dataclass(frozen=True)
class Installed:
    database: str
    owner_url: str
    runtime_url: str
    role: str
    password: str


def _run(monkeypatch, env: dict[str, str], *args: str) -> str:
    """`farmcore <args>` with this environment; its output (the command must succeed)."""
    from farmcore.cli import cli

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, list(args), catch_exceptions=False)
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    return result.output


def _migrate(monkeypatch, env: dict[str, str]) -> None:
    _run(monkeypatch, env, "migrate")


@pytest.fixture
def install(pg_server, monkeypatch) -> Iterator:
    """`farmcore migrate` a new database with its own runtime role; dropped afterwards."""
    made: list[Installed] = []
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")

    def make(password: str | None = None, before=None) -> Installed:
        """`before(admin_connection, role)` runs before `farmcore migrate`."""
        name = f"inst_{uuid.uuid4().hex[:10]}"
        role = f"rt_{uuid.uuid4().hex[:10]}"
        password = password or secrets.token_hex(16)
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            if before is not None:
                before(conn, role)
        with create_engine(pg_server.url(name)).begin() as conn:
            conn.execute(text("CREATE EXTENSION postgis"))
        owner = pg_server.url(name)
        made.append(Installed(name, owner, pg_server.url(name, role, password), role, password))
        _migrate(
            monkeypatch,
            {
                "DATABASE_URL": owner,
                "RUNTIME_DATABASE_ROLE": role,
                "RUNTIME_DATABASE_PASSWORD": password,
            },
        )
        return made[-1]

    yield make
    with admin.connect() as conn:
        for inst in made:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{inst.database}" WITH (FORCE)'))
        for inst in made:
            exists = text("SELECT 1 FROM pg_roles WHERE rolname = :r")
            if conn.execute(exists, {"r": inst.role}).scalar() is None:
                continue
            conn.execute(text(f'DROP OWNED BY "{inst.role}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{inst.role}"'))
    admin.dispose()


def _denied(url: str, sql: str) -> None:
    # Autocommit: CREATE DATABASE refuses a transaction block before it checks privileges.
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect() as conn:
            conn.execute(text(sql))
        assert isinstance(exc.value.orig, psycopg.errors.InsufficientPrivilege), sql
    finally:
        engine.dispose()


def test_runtime_role_reads_and_writes_rows(install) -> None:
    inst = install()
    engine = create_engine(inst.runtime_url)
    try:
        with engine.begin() as conn:
            farm_id = conn.execute(
                text("INSERT INTO farm (name) VALUES ('Runtime farm') RETURNING id")
            ).scalar_one()
            conn.execute(text("UPDATE farm SET name = 'Renamed' WHERE id = :i"), {"i": farm_id})
            assert conn.execute(text("SELECT count(*) FROM farm")).scalar_one() == 1
            conn.execute(text("DELETE FROM farm WHERE id = :i"), {"i": farm_id})
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE DATABASE runtime_made",
        "CREATE ROLE runtime_made",
        "CREATE EXTENSION pgcrypto",
        "CREATE TABLE public.runtime_made (id int)",
        "CREATE SCHEMA runtime_made",
        "CREATE TEMP TABLE runtime_made (id int)",
        "ALTER TABLE farm ADD COLUMN runtime_made int",
        "DROP TABLE farm CASCADE",
        "TRUNCATE farm",
        "CREATE INDEX runtime_made ON farm (name)",
        "UPDATE alembic_version SET version_num = 'runtime_made'",
        "DELETE FROM alembic_version",
    ],
)
def test_runtime_role_cannot_change_schema_roles_or_databases(install, sql) -> None:
    _denied(install().runtime_url, sql)


def test_runtime_role_cannot_change_the_installation_id(install) -> None:
    inst = install()
    _denied(
        inst.runtime_url,
        f"ALTER DATABASE \"{inst.database}\" SET farmcore.installation_id = '{uuid.uuid4()}'",
    )
    _denied(inst.runtime_url, f'ALTER ROLE "{inst.role}" CREATEDB')


def test_tables_added_by_a_later_migration_are_usable_by_the_runtime(install) -> None:
    inst = install()
    with create_engine(inst.owner_url).begin() as conn:
        conn.execute(text("CREATE TABLE later_table (id bigserial PRIMARY KEY, v text)"))
    engine = create_engine(inst.runtime_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO later_table (v) VALUES ('x')"))
            assert conn.execute(text("SELECT v FROM later_table")).scalar_one() == "x"
    finally:
        engine.dispose()


def test_one_installations_runtime_cannot_connect_to_another_database(install, pg_server) -> None:
    """Even on one shared server, A's runtime credentials cannot open B's database."""
    a, b = install(), install()
    engine = create_engine(pg_server.url(b.database, a.role, a.password))
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect():
            pass
        assert "permission denied for database" in str(exc.value)
    finally:
        engine.dispose()


def test_migrate_is_repeatable_and_keeps_the_runtime_password(install, monkeypatch) -> None:
    inst = install()
    password = inst.password
    _migrate(
        monkeypatch,
        {
            "DATABASE_URL": inst.owner_url,
            "RUNTIME_DATABASE_ROLE": inst.role,
            "RUNTIME_DATABASE_PASSWORD": password,
        },
    )
    engine = create_engine(inst.runtime_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM farm")).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("database", ["postgres", "template1", "template_postgis"])
def test_runtime_role_cannot_open_the_servers_own_databases(install, pg_server, database) -> None:
    inst = install()
    engine = create_engine(pg_server.url(database, inst.role, inst.password))
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect():
            pass
        assert "permission denied for database" in str(exc.value)
    finally:
        engine.dispose()


def test_a_password_with_sql_and_format_characters_works_and_is_never_echoed(
    install, pg_server, capsys
) -> None:
    password = "p%s%%'\";--" + secrets.token_hex(8)
    inst = install(password=password)
    engine = create_engine(inst.runtime_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar_one() == inst.role
    finally:
        engine.dispose()
    out = capsys.readouterr()
    assert password not in out.out + out.err
    assert inst.role in pg_server.log()  # the role statements are logged ...
    assert password not in pg_server.log()  # ... but never with the password
    # Only a SCRAM verifier reaches the server: the plaintext is never in the statement.
    admin = create_engine(pg_server.url("farmcore"))
    try:
        with admin.connect() as conn:
            stored = conn.execute(
                text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"), {"r": inst.role}
            ).scalar_one()
    finally:
        admin.dispose()
    assert stored.startswith("SCRAM-SHA-256$")


def _migrate_fails(monkeypatch, env: dict[str, str]) -> str:
    from farmcore.cli import cli

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["migrate"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code != 0
    text_ = result.output + (str(result.exception) if result.exception else "")
    return text_


def test_a_failing_role_statement_never_echoes_the_password(
    install, monkeypatch, pg_server
) -> None:
    inst = install()
    password = "never-shown-" + secrets.token_hex(8)
    out = _migrate_fails(
        monkeypatch,
        {
            "DATABASE_URL": inst.owner_url,
            "RUNTIME_DATABASE_ROLE": "pg_monitor",  # reserved: ALTER ROLE fails server-side
            "RUNTIME_DATABASE_PASSWORD": password,
        },
    )
    assert "runtime role" in out
    assert password not in out and "SCRAM" not in out and "PASSWORD" not in out
    assert password not in pg_server.log()  # not in the failing statement the server logs


def test_migrate_refuses_to_run_as_the_runtime_role(install, monkeypatch) -> None:
    inst = install()
    out = _migrate_fails(monkeypatch, {"DATABASE_URL": inst.runtime_url})
    assert "database owner" in out


def test_migrate_refuses_a_runtime_role_named_like_the_owner(install, monkeypatch) -> None:
    inst = install()
    out = _migrate_fails(
        monkeypatch,
        {
            "DATABASE_URL": inst.owner_url,
            "RUNTIME_DATABASE_ROLE": "farmcore",
            "RUNTIME_DATABASE_PASSWORD": secrets.token_hex(16),
        },
    )
    assert "must differ from the owner" in out


def test_an_existing_runtime_role_loses_memberships_and_role_settings(install) -> None:
    """A pre-existing role granted the owner could otherwise SET ROLE to it and run DDL."""

    def pre_create(conn, role: str) -> None:
        conn.execute(text(f'CREATE ROLE "{role}" LOGIN'))
        conn.execute(text(f'GRANT farmcore, pg_read_all_data TO "{role}"'))
        conn.execute(text(f'ALTER ROLE "{role}" SET search_path = pg_catalog'))

    inst = install(before=pre_create)
    _denied(inst.runtime_url, "SET ROLE farmcore")
    engine = create_engine(inst.runtime_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SHOW search_path")).scalar_one() != "pg_catalog"
            members = conn.execute(
                text(
                    "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                    "WHERE r.rolname = current_user"
                )
            ).scalar_one()
            assert members == 0
    finally:
        engine.dispose()


def _remigrate(monkeypatch, inst: Installed) -> None:
    _migrate(
        monkeypatch,
        {
            "DATABASE_URL": inst.owner_url,
            "RUNTIME_DATABASE_ROLE": inst.role,
            "RUNTIME_DATABASE_PASSWORD": inst.password,
        },
    )


def test_migrate_drops_privileges_granted_to_the_runtime_role_directly(
    install, monkeypatch, pg_server
) -> None:
    inst = install()
    r = f'"{inst.role}"'
    with create_engine(inst.owner_url).begin() as conn:
        conn.execute(text(f'GRANT CREATE ON DATABASE "{inst.database}" TO {r}'))
        conn.execute(text(f"GRANT ALL ON SCHEMA public TO {r}"))
        conn.execute(text(f"GRANT TRUNCATE, REFERENCES, TRIGGER ON farm TO {r}"))
    with create_engine(pg_server.url("postgres")).begin() as conn:
        conn.execute(text(f"GRANT CONNECT, TEMPORARY, CREATE ON DATABASE postgres TO {r}"))
    _remigrate(monkeypatch, inst)
    for sql in (
        "CREATE TABLE public.runtime_made (id int)",
        "CREATE SCHEMA runtime_made",
        "TRUNCATE farm",
        "CREATE TRIGGER t BEFORE INSERT ON farm FOR EACH ROW "
        "EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    ):
        _denied(inst.runtime_url, sql)
    engine = create_engine(pg_server.url("postgres", inst.role, inst.password))
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect():
            pass
        assert "permission denied for database" in str(exc.value)
    finally:
        engine.dispose()


def test_migrate_refuses_a_runtime_role_that_owns_objects(install, monkeypatch) -> None:
    inst = install()
    with create_engine(inst.owner_url).begin() as conn:
        conn.execute(text("CREATE TABLE owned_by_runtime (id int)"))
        conn.execute(text(f'ALTER TABLE owned_by_runtime OWNER TO "{inst.role}"'))
    out = _migrate_fails(
        monkeypatch,
        {
            "DATABASE_URL": inst.owner_url,
            "RUNTIME_DATABASE_ROLE": inst.role,
            "RUNTIME_DATABASE_PASSWORD": inst.password,
        },
    )
    assert f"{inst.role} owns" in out and "must own nothing" in out


def test_a_membership_granted_by_another_role_is_removed_too(install, pg_server) -> None:
    """PostgreSQL 16+: a plain REVOKE only removes grants made by the revoking role."""
    granter = f"granter_{uuid.uuid4().hex[:8]}"

    def pre_create(conn, role: str) -> None:
        conn.execute(text(f'CREATE ROLE "{role}" LOGIN'))
        conn.execute(text(f'CREATE ROLE "{granter}"'))
        conn.execute(text(f'GRANT pg_read_all_data TO "{granter}" WITH ADMIN OPTION'))
        conn.execute(text(f'GRANT pg_read_all_data TO "{role}" GRANTED BY "{granter}"'))

    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    try:
        inst = install(before=pre_create)
        with admin.connect() as conn:
            left = conn.execute(
                text(
                    "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                    "WHERE r.rolname = :r"
                ),
                {"r": inst.role},
            ).scalar_one()
        assert left == 0
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP ROLE IF EXISTS "{granter}"'))
        admin.dispose()


def test_migrate_fails_if_a_membership_survives_the_revoke(install, monkeypatch) -> None:
    """Fault injection: the catalog keeps reporting a membership after it was revoked (e.g. one
    granted concurrently). Migrate must stop rather than leave a privileged runtime role."""
    from farmcore.core.commands import migrate

    inst = install()
    monkeypatch.setattr(
        migrate, "_MEMBERSHIPS", text("SELECT 'pg_monitor'::text, current_user::text WHERE :r = :r")
    )
    out = _migrate_fails(
        monkeypatch,
        {
            "DATABASE_URL": inst.owner_url,
            "RUNTIME_DATABASE_ROLE": inst.role,
            "RUNTIME_DATABASE_PASSWORD": inst.password,
        },
    )
    assert f"{inst.role} is still a member of pg_monitor" in out


def test_a_non_superuser_owner_leaves_the_servers_databases_alone(pg_server, monkeypatch) -> None:
    """On a shared server the owner is an ordinary role: migrate then changes only what it
    owns, never the server-wide rights on `postgres` and the templates."""
    suffix = uuid.uuid4().hex[:8]
    owner, role, database = f"own_{suffix}", f"rt_{suffix}", f"shared_{suffix}"
    owner_pw, role_pw = secrets.token_hex(16), secrets.token_hex(16)
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    acl = text("SELECT datacl::text FROM pg_database WHERE datname = :d")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{owner_pw}'"))
            conn.execute(text(f"CREATE DATABASE {database} OWNER {owner}"))
            for d in SERVER_DBS:
                conn.execute(text(f"GRANT CONNECT, TEMPORARY ON DATABASE {d} TO PUBLIC"))
            before = {d: conn.execute(acl, {"d": d}).scalar() for d in SERVER_DBS}
        with create_engine(pg_server.url(database)).begin() as conn:
            conn.execute(text("CREATE EXTENSION postgis"))
            conn.execute(text(f"ALTER SCHEMA public OWNER TO {owner}"))
        env = {
            "DATABASE_URL": pg_server.url(database, owner, owner_pw),
            "RUNTIME_DATABASE_ROLE": role,
            "RUNTIME_DATABASE_PASSWORD": role_pw,
        }
        # A non-superuser may set the identity only once a superuser allows it, which the
        # refusal says how to do.
        refused = _migrate_fails(monkeypatch, env)
        assert f"GRANT SET ON PARAMETER farmcore.installation_id TO {owner}" in refused
        with admin.connect() as conn:
            conn.execute(text(f"GRANT SET ON PARAMETER farmcore.installation_id TO {owner}"))
        _migrate(monkeypatch, env)
        with admin.connect() as conn:
            assert {d: conn.execute(acl, {"d": d}).scalar() for d in SERVER_DBS} == before
        _denied(pg_server.url(database, role, role_pw), "CREATE TABLE public.t (id int)")
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
            conn.execute(text(f"REVOKE SET ON PARAMETER farmcore.installation_id FROM {owner}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {owner}"))
        admin.dispose()


SERVER_DBS = ("postgres", "template1", "template_postgis")


def test_a_session_override_of_the_setting_does_not_change_the_reported_id(install) -> None:
    from fastapi.testclient import TestClient

    from farmcore.main import create_app
    from farmcore.settings import Settings

    inst = install()
    with TestClient(create_app(Settings(_env_file=None, database_url=inst.runtime_url))) as c:
        real = c.get("/health").json()["installation_id"]
    forged = str(uuid.uuid4())
    url = f"{inst.runtime_url}?options=-c%20farmcore.installation_id%3D{forged}"
    probe = create_engine(url)
    try:
        with probe.connect() as conn:  # the override is live in these sessions
            assert conn.execute(text("SHOW farmcore.installation_id")).scalar_one() == forged
    finally:
        probe.dispose()
    with TestClient(create_app(Settings(_env_file=None, database_url=url))) as c:
        assert c.get("/health").json()["installation_id"] == real


SIGNED_IN = "You are signed in as admin."
BASE = "https://testserver"


def _app(url: str, token: str):
    from pydantic import SecretStr

    from farmcore.main import create_app
    from farmcore.settings import Settings

    settings = Settings(_env_file=None, database_url=url, admin_token=SecretStr(token))
    return create_app(settings, app_packages=())


def _signed_in(client) -> bool:
    return SIGNED_IN in client.get("/admin/sign-in").text


def _sign_in(client, token: str) -> None:
    resp = client.post(
        "/admin/sign-in", data={"token": token}, headers={"Origin": BASE}, follow_redirects=False
    )
    assert resp.status_code == 303


def _share(src, dst) -> None:
    """One browser, one host: every cookie either installation set is sent to both."""
    for cookie in src.cookies.jar:
        dst.cookies.jar.set_cookie(cookie)


def test_a_cookie_from_one_installation_never_signs_in_to_another(install) -> None:
    from fastapi.testclient import TestClient

    a, b = install(), install()
    token = secrets.token_hex(24)  # the operator reused one admin token for both
    with (
        TestClient(_app(a.runtime_url, token), base_url=BASE) as ca,
        TestClient(_app(b.runtime_url, token), base_url=BASE) as cb,
    ):
        _sign_in(ca, token)
        _share(ca, cb)
        assert _signed_in(ca)
        assert not _signed_in(cb)


def test_signing_in_to_one_installation_keeps_the_other_signed_in(install) -> None:
    from fastapi.testclient import TestClient

    a, b = install(), install()
    ta, tb = secrets.token_hex(24), secrets.token_hex(24)
    with (
        TestClient(_app(a.runtime_url, ta), base_url=BASE) as ca,
        TestClient(_app(b.runtime_url, tb), base_url=BASE) as cb,
    ):
        _sign_in(ca, ta)
        _share(ca, cb)
        _sign_in(cb, tb)
        _share(cb, ca)
        assert _signed_in(ca) and _signed_in(cb)
        assert len({c.name for c in ca.cookies.jar}) == 2


def _prefix(installation_id: str) -> str:
    """The documented scheme: `fc-<first 12 hex digits of installation_id>-`."""
    return f"fc-{uuid.UUID(installation_id).hex[:12]}-"


def test_only_this_installations_cookies_reach_the_app(install) -> None:
    from fastapi.testclient import TestClient

    token = secrets.token_hex(24)
    app = _app(install().runtime_url, token)

    @app.get("/_cookies")
    def cookies(request: Request) -> dict[str, str]:
        return dict(request.cookies)

    with TestClient(app, base_url=BASE, headers={"X-Admin-Token": token}) as c:
        own = _prefix(c.get("/health").json()["installation_id"])
        c.cookies.set(f"{own}farmcore_notifications_read_1", "abc")
        c.cookies.set("farmcore_notifications_read_2", "plain-name")
        c.cookies.set("farmcore_session", "foreign-or-legacy")
        c.cookies.set("fc-000000000000-farmcore_session", "other-installation")
        assert c.get("/_cookies").json() == {"farmcore_notifications_read_1": "abc"}


def test_the_notification_read_cookie_is_scoped_per_installation(install, monkeypatch) -> None:
    """bell.js writes the read-state cookie itself, under the name the page gives it."""
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from farmcore.main import create_app
    from farmcore.settings import Settings

    names = []
    for inst in (install(), install()):
        created = _run(monkeypatch, {"DATABASE_URL": inst.runtime_url}, "farms", "create", "F")
        farm_id = created.split()[1]
        token = secrets.token_hex(24)
        settings = Settings(
            _env_file=None, database_url=inst.runtime_url, admin_token=SecretStr(token)
        )
        with TestClient(create_app(settings), base_url=BASE, headers={"X-Admin-Token": token}) as c:
            own = _prefix(c.get("/health").json()["installation_id"])
            page = c.get(f"/notifications?farm={farm_id}").text
        name = page.split('data-cookie="')[1].split('"')[0]
        assert name == f"{own}farmcore_notifications_read_{farm_id}"
        names.append(name)
    assert names[0] != names[1]


# --- Two real Compose projects from one image -------------------------------------------------


@pytest.fixture(scope="module")
def two_installations(new_project) -> tuple[Project, Project]:
    """Projects A and B from one image, each with its own name, env file and secrets."""
    a, b = new_project(), new_project()
    a.up()
    b.up()
    return a, b


@pytest.mark.slow
def test_two_projects_run_side_by_side_with_their_own_identity(two_installations) -> None:
    a, b = two_installations
    ha, hb = a.health(), b.health()
    assert ha["db"] == hb["db"] == "ok"
    assert uuid.UUID(ha["installation_id"]) != uuid.UUID(hb["installation_id"])


@pytest.mark.slow
def test_projects_share_no_network_volume_or_bind_mount(two_installations) -> None:
    a, b = two_installations

    def resources(p: Project) -> tuple[set[str], set[str]]:
        networks, mounts = set(), set()
        for cid in p.compose("ps", "-a", "-q").stdout.split():
            info = inspect(cid)
            networks |= set(info["NetworkSettings"]["Networks"])
            mounts |= {m.get("Name") or m["Source"] for m in info["Mounts"]}
        return networks, mounts

    (na, ma), (nb, mb) = resources(a), resources(b)
    assert na and nb and not na & nb
    assert ma and mb and not ma & mb
    for info in map(inspect, (a.container("app"), b.container("app"))):
        assert all(m["Type"] == "volume" for m in info["Mounts"]), info["Mounts"]


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
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE DATABASE runtime_made",
        "CREATE ROLE runtime_made",
        "CREATE EXTENSION pgcrypto",
        "CREATE TABLE runtime_made (id int)",
        "ALTER TABLE farm ADD COLUMN runtime_made int",
    ],
)
def test_runtime_role_in_a_project_cannot_change_the_schema(two_installations, sql) -> None:
    a, _ = two_installations
    out = _psql(a, "farmcore_app", a.env["RUNTIME_DATABASE_PASSWORD"], sql)
    assert "permission denied" in out or "must be owner" in out, out


@pytest.mark.slow
def test_stopping_one_project_leaves_the_other_healthy(two_installations) -> None:
    a, b = two_installations
    before = b.health()["installation_id"]
    a.compose("stop")
    try:
        assert b.health() == {"db": "ok", "installation_id": before}
        state = inspect(b.container("app"))["State"]
        assert state["Running"] and state["Health"]["Status"] == "healthy"
    finally:
        a.compose("start")


@pytest.mark.slow
def test_no_container_leaves_an_anonymous_volume_behind(two_installations) -> None:
    """`docker compose down` without `-v` must leave only the project's named volumes."""
    a, _ = two_installations
    for cid in a.compose("ps", "-a", "-q").stdout.split():
        info = inspect(cid)
        for mount in info["Mounts"]:
            if mount["Type"] == "volume":
                assert mount["Name"].startswith(f"{a.name}_"), (info["Name"], mount)


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
