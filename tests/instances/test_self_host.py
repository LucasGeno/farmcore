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
    assert "need Crop protection. Switch them off first." in refused.output

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


def test_the_cli_and_the_apps_page_refuse_the_same_switch_with_the_same_words(runtime, cli):
    """Advisor stays installed, but Stations is off (edited in the table): Advisor will not
    load, so switching on Review, which needs Advisor, must be refused by both."""
    from pydantic import SecretStr
    from sqlalchemy import create_engine, text

    from farmcore.main import create_app

    env = {"DATABASE_URL": runtime}
    assert cli("apps", "--uninstall", "review", env=env).exit_code == 0
    engine = create_engine(runtime)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app_module (name, version, state) VALUES ('stations', '0', "
                "'uninstalled') ON CONFLICT (name) DO UPDATE SET state = 'uninstalled'"
            )
        )
    engine.dispose()

    refused = cli("apps", "--install", "review", env=env)
    token = secrets.token_hex(24)
    settings = Settings(_env_file=None, database_url=runtime, admin_token=SecretStr(token))
    with TestClient(create_app(settings), headers={"X-Admin-Token": token}) as c:
        page = c.post("/apps/review/install")
    assert page.status_code == 409
    assert refused.exit_code == 1
    assert page.json()["detail"] in refused.output


def test_switching_on_an_app_names_every_dependency_that_will_not_load(runtime, cli):
    from sqlalchemy import create_engine, text

    env = {"DATABASE_URL": runtime}
    off = ("forecast", "chat", "kpi", "review", "advisor")
    args = [a for name in off for a in ("--uninstall", name)]
    assert cli("apps", *args, env=env).exit_code == 0
    engine = create_engine(runtime)
    with engine.begin() as conn:  # Stations off as well, by editing the table
        conn.execute(
            text(
                "INSERT INTO app_module (name, version, state) VALUES ('stations', '0', "
                "'uninstalled') ON CONFLICT (name) DO UPDATE SET state = 'uninstalled'"
            )
        )
    engine.dispose()
    refused = cli("apps", "--install", "forecast", env=env)
    assert refused.exit_code == 1
    assert refused.output.strip().endswith("and Crop protection. Switch them on first.")


def test_installing_and_uninstalling_one_app_at_once_is_an_error(runtime, cli):
    both = cli("apps", "--install", "kpi", "--uninstall", "kpi", env={"DATABASE_URL": runtime})
    assert both.exit_code == 2 and "both" in both.output


@pytest.fixture
def unmigrated(pg_server, fresh_db) -> str:
    """Schema at head, but `farmcore migrate` never ran: no installation id."""
    from alembic import command

    from farmcore.core.commands.migrate import alembic_config

    url = pg_server.url(fresh_db)
    command.upgrade(alembic_config(url), "head")
    return url


def _admin_cookie_names(settings: Settings) -> list[str]:
    from farmcore.main import create_app

    token = settings.admin_token.get_secret_value()
    with TestClient(create_app(settings, app_packages=()), base_url="https://testserver") as c:
        resp = c.post(
            "/admin/sign-in",
            data={"token": token},
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        return [h.split("=", 1)[0] for h in resp.headers.get_list("set-cookie")]


def test_a_public_app_without_an_installation_id_still_scopes_its_cookies(unmigrated, caplog):
    from pydantic import SecretStr

    token = secrets.token_hex(24)
    settings = Settings(
        _env_file=None, database_url=unmigrated, farmcore_public=True, admin_token=SecretStr(token)
    )
    with caplog.at_level("WARNING", logger="farmcore.main"):
        first = _admin_cookie_names(settings)
        second = _admin_cookie_names(settings)
    assert "farmcore migrate" in caplog.text and "installation_id" in caplog.text
    assert all(n.startswith("fc-") and n.endswith("-admin_token") for n in first + second)
    # A per-process scope: a restart signs everyone out rather than sharing a cookie.
    assert first != second


def test_a_local_app_without_an_installation_id_warns(unmigrated, caplog):
    from farmcore.main import create_app

    with caplog.at_level("WARNING", logger="farmcore.main"):
        create_app(Settings(_env_file=None, database_url=unmigrated), app_packages=())
    assert "farmcore migrate" in caplog.text


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
