"""One self-hosted installation through its public seams: the `farmcore` CLI and HTTP.

Every test gets its own empty database on a throwaway PostGIS server (see conftest.py).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner, Result

from farmcore.settings import Settings, get_settings


@pytest.fixture
def cli(monkeypatch) -> Iterator:
    """Run `farmcore ...` with the given environment, as an operator would."""

    def run(*args: str, env: dict[str, str], input: str | None = None) -> Result:
        from farmcore.cli import cli as app

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        try:
            return CliRunner().invoke(app, list(args), input=input, catch_exceptions=False)
        finally:
            get_settings.cache_clear()

    yield run


def _health(database_url: str) -> dict:
    from farmcore.main import create_app

    app = create_app(Settings(_env_file=None, database_url=database_url), manifests=[])
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    return resp.json()


def test_migrate_generates_one_installation_id_that_health_reports(pg_server, fresh_db, cli):
    owner = pg_server.url(fresh_db)
    assert cli("migrate", env={"DATABASE_URL": owner}).exit_code == 0

    first = _health(owner)["installation_id"]
    assert uuid.UUID(first).version == 4

    # A second migrate (an upgrade) and an app restart keep the identity.
    assert cli("migrate", env={"DATABASE_URL": owner}).exit_code == 0
    assert _health(owner)["installation_id"] == first


def test_a_cloned_database_gets_its_own_installation_id(pg_server, fresh_db, cli):
    """Copying a migrated database (same tables, same rows) never copies its identity."""
    from sqlalchemy import create_engine, text

    assert cli("migrate", env={"DATABASE_URL": pg_server.url(fresh_db)}).exit_code == 0
    a = _health(pg_server.url(fresh_db))["installation_id"]
    other = f"{fresh_db}_b"
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{other}" TEMPLATE "{fresh_db}"'))
    try:
        assert cli("migrate", env={"DATABASE_URL": pg_server.url(other)}).exit_code == 0
        b = _health(pg_server.url(other))["installation_id"]
        assert uuid.UUID(b).version == 4
        assert a != b
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{other}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def runtime(pg_server, fresh_db, cli) -> Iterator[str]:
    """A migrated installation; yields its runtime-role URL (what app, bot and CLI use)."""
    from sqlalchemy import create_engine, text

    role, password = f"rt_{uuid.uuid4().hex[:10]}", secrets.token_hex(16)
    env = {
        "DATABASE_URL": pg_server.url(fresh_db),
        "RUNTIME_DATABASE_ROLE": role,
        "RUNTIME_DATABASE_PASSWORD": password,
    }
    assert cli("migrate", env=env).exit_code == 0
    yield pg_server.url(fresh_db, role, password)
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{fresh_db}" WITH (FORCE)'))
        conn.execute(text(f'DROP OWNED BY "{role}"'))
        conn.execute(text(f'DROP ROLE "{role}"'))
    admin.dispose()


def test_a_fresh_installation_starts_empty_and_is_set_up_through_the_cli(runtime, cli):
    env = {"DATABASE_URL": runtime}
    listed = cli("farms", "list", env=env)
    assert listed.exit_code == 0 and listed.output.strip() == "no farms"

    created = cli("farms", "create", "Ferme des Deux Rives", env=env)
    assert created.exit_code == 0, created.output
    farm_id = created.output.split()[1]
    assert cli("farms", "list", env=env).output.split() == [
        farm_id,
        "Ferme",
        "des",
        "Deux",
        "Rives",
    ]

    password = secrets.token_urlsafe(18)
    user = cli(
        "users", "create", "alice", "--farm-id", farm_id, "--password-stdin",
        env=env, input=password + "\n",
    )  # fmt: skip
    assert user.exit_code == 0, user.output

    # The member signs in over HTTP on the app started with the runtime role.
    from farmcore.main import create_app

    base = "https://testserver"
    app = create_app(Settings(_env_file=None, database_url=runtime))
    with TestClient(app, base_url=base) as c:
        page = c.get("/login")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        resp = c.post(
            "/login",
            data={"login": "alice", "password": password, "csrf_token": csrf},
            headers={"Origin": base},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        assert c.get(f"/today/?farm={farm_id}").status_code == 200


def test_optional_apps_are_switched_through_the_cli(runtime, cli):
    env = {"DATABASE_URL": runtime}
    off = cli("apps", "--uninstall", "assistant", env=env)
    assert off.exit_code == 0, off.output
    assert "assistant" in off.output and "uninstalled" in off.output

    refused = cli("apps", "--uninstall", "advisor", env=env)
    assert refused.exit_code == 1
    assert "needs advisor" in refused.output

    required = cli("apps", "--uninstall", "today", env=env)
    assert required.exit_code == 1
    assert "always on" in required.output

    on = cli("apps", "--install", "assistant", env=env)
    assert on.exit_code == 0, on.output
    row = [line for line in on.output.splitlines() if " assistant " in line]
    assert row and "installed" in row[0].split() and "uninstalled" not in row[0]


def test_the_setup_cli_refuses_unknown_apps_and_unnamed_farms(runtime, cli):
    env = {"DATABASE_URL": runtime}
    unknown = cli("apps", "--uninstall", "no_such_app", env=env)
    assert unknown.exit_code == 2 and "no app named 'no_such_app'" in unknown.output
    blank = cli("farms", "create", "   ", env=env)
    assert blank.exit_code == 2 and "A farm needs a name." in blank.output
    assert cli("farms", "list", env=env).output.strip() == "no farms"


OFFLINE = """\
# No route out of the project network: no hosting service, subscription endpoint or AI provider.
networks:
  default:
    internal: true
services:
  app:
    ports: !reset []
"""


@pytest.mark.slow
def test_an_installation_runs_with_no_network_access_and_no_demo_data(new_project, tmp_path):
    override = tmp_path / "offline.yml"
    override.write_text(OFFLINE)
    project = new_project(override)
    project.up()

    health = project.exec_health()
    assert health["db"] == "ok" and uuid.UUID(health["installation_id"])
    egress = project.compose(
        "exec", "-T", "app", "python", "-c",
        "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)",
        check=False,
    )  # fmt: skip
    assert egress.returncode != 0
    farms = project.compose("exec", "-T", "app", "farmcore", "farms", "list")
    assert farms.stdout.strip() == "no farms"
    created = project.compose("exec", "-T", "app", "farmcore", "farms", "create", "Offline farm")
    assert created.stdout.startswith("farm ")
