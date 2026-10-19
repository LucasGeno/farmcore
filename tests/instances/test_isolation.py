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
from tests.instances.conftest import Project, inspect, postgis_image


@dataclass(frozen=True)
class Installed:
    database: str
    owner_url: str
    runtime_url: str
    role: str


def _migrate(monkeypatch, env: dict[str, str]) -> None:
    from farmcore.cli import cli

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["migrate"], catch_exceptions=False)
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output


@pytest.fixture
def install(pg_server, monkeypatch) -> Iterator:
    """`farmcore migrate` a new database with its own runtime role; dropped afterwards."""
    made: list[Installed] = []
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")

    def make() -> Installed:
        name = f"inst_{uuid.uuid4().hex[:10]}"
        role = f"rt_{uuid.uuid4().hex[:10]}"
        password = secrets.token_hex(16)
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        with create_engine(pg_server.url(name)).begin() as conn:
            conn.execute(text("CREATE EXTENSION postgis"))
        owner = pg_server.url(name)
        _migrate(
            monkeypatch,
            {
                "DATABASE_URL": owner,
                "RUNTIME_DATABASE_ROLE": role,
                "RUNTIME_DATABASE_PASSWORD": password,
            },
        )
        made.append(Installed(name, owner, pg_server.url(name, role, password), role))
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
    role_a, password_a = a.role, a.runtime_url.split(":")[2].split("@")[0]
    engine = create_engine(pg_server.url(b.database, role_a, password_a))
    try:
        with pytest.raises(DBAPIError) as exc, engine.connect():
            pass
        assert "permission denied for database" in str(exc.value)
    finally:
        engine.dispose()


def test_migrate_is_repeatable_and_keeps_the_runtime_password(install, monkeypatch) -> None:
    inst = install()
    password = inst.runtime_url.split(":")[2].split("@")[0]
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


def test_a_browser_written_preference_cookie_still_reaches_the_app(install) -> None:
    """bell.js writes `farmcore_notifications_read_<farm>` itself, under its plain name."""
    from fastapi.testclient import TestClient

    token = secrets.token_hex(24)
    app = _app(install().runtime_url, token)

    @app.get("/_cookies")
    def cookies(request: Request) -> dict[str, str]:
        return dict(request.cookies)

    with TestClient(app, base_url=BASE, headers={"X-Admin-Token": token}) as c:
        c.cookies.set("farmcore_notifications_read_1", "abc")
        c.cookies.set("farmcore_session", "foreign-or-legacy")
        c.cookies.set("fc-000000000000-farmcore_session", "other-installation")
        assert c.get("/_cookies").json() == {"farmcore_notifications_read_1": "abc"}


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
