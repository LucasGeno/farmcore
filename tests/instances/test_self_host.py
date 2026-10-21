"""One self-hosted installation set up through its public seams: the `farmcore` CLI and HTTP.

Every test gets its own migrated database on a throwaway PostGIS server (see conftest.py).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from farmcore.settings import Settings
from tests.instances.support import run_cli


@pytest.fixture
def runtime(pg_server, fresh_db) -> Iterator[str]:
    """A migrated installation; yields its runtime-role URL (what app, bot and CLI use)."""
    from sqlalchemy import create_engine, text

    role, password = f"rt_{uuid.uuid4().hex[:10]}", secrets.token_hex(16)
    env = {
        "DATABASE_URL": pg_server.url(fresh_db),
        "RUNTIME_DATABASE_ROLE": role,
        "RUNTIME_DATABASE_PASSWORD": password,
    }
    assert run_cli("migrate", env=env).exit_code == 0
    yield pg_server.url(fresh_db, role, password)
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{fresh_db}" WITH (FORCE)'))
        conn.execute(text(f'DROP OWNED BY "{role}"'))
        conn.execute(text(f'DROP ROLE "{role}"'))
    admin.dispose()


def test_a_fresh_installation_starts_empty_and_is_set_up_through_the_cli(runtime):
    """A demo-free install must be set up without the UI, and a CLI-made farm must be open to
    its member and to the operator."""
    from pydantic import SecretStr

    from farmcore.main import create_app

    env = {"DATABASE_URL": runtime}
    listed = run_cli("farms", "list", env=env)
    assert listed.exit_code == 0 and listed.output.strip() == "no farms"
    blank = run_cli("farms", "create", "   ", env=env)
    assert blank.exit_code != 0 and run_cli("farms", "list", env=env).output.strip() == "no farms"

    created = run_cli("farms", "create", "Ferme des Deux Rives", env=env)
    assert created.exit_code == 0, created.output
    farm_id = created.output.split()[1]
    assert run_cli("farms", "list", env=env).output.split() == [
        farm_id, "Ferme", "des", "Deux", "Rives",
    ]  # fmt: skip
    password = secrets.token_urlsafe(18)
    user = run_cli(
        "users", "create", "alice", "--farm-id", farm_id, "--password-stdin",
        env=env, input=password + "\n",
    )  # fmt: skip
    assert user.exit_code == 0, user.output

    # The member signs in over HTTP on the app started with the runtime role.
    base = "https://testserver"
    with TestClient(create_app(Settings(_env_file=None, database_url=runtime)), base_url=base) as c:
        page = c.get("/login")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        resp = c.post(
            "/login",
            data={"login": "alice", "password": password, "csrf_token": csrf},
            headers={"Origin": base},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        assert c.get(f"/notifications?farm={farm_id}").status_code == 200
    # ...and so does the operator, with the admin token.
    token = secrets.token_hex(24)
    settings = Settings(_env_file=None, database_url=runtime, admin_token=SecretStr(token))
    with TestClient(create_app(settings), headers={"X-Admin-Token": token}) as c:
        assert c.get(f"/notifications?farm={farm_id}").status_code == 200


def _row(listing: str, app: str) -> list[str]:
    return next(line for line in listing.splitlines() if f" {app} " in line).split()


def test_optional_apps_are_switched_through_the_cli(runtime):
    env = {"DATABASE_URL": runtime}
    off = run_cli("apps", "--uninstall", "assistant", env=env)
    assert off.exit_code == 0, off.output
    assert "uninstalled" in _row(off.output, "assistant")

    dependents = run_cli("apps", "--uninstall", "advisor", env=env)  # others need it
    required = run_cli("apps", "--uninstall", "today", env=env)  # always on
    unknown = run_cli("apps", "--uninstall", "no_such_app", env=env)
    assert dependents.exit_code == 1 and required.exit_code == 1
    assert unknown.exit_code != 0 and "no_such_app" in unknown.output
    listing = run_cli("apps", env=env).output
    assert "installed" in _row(listing, "advisor") and "installed" in _row(listing, "today")

    on = run_cli("apps", "--install", "assistant", env=env)
    assert on.exit_code == 0, on.output
    assert "installed" in _row(on.output, "assistant")


def test_switches_apply_all_together_or_not_at_all(runtime):
    env = {"DATABASE_URL": runtime}

    refused = run_cli("apps", "--uninstall", "assistant", "--uninstall", "today", env=env)

    assert refused.exit_code == 1
    assert "installed" in _row(run_cli("apps", env=env).output, "assistant")


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
