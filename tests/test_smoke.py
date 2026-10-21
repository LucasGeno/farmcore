"""Scaffold smoke tests: app factory, kernel migration, templates, CLI, settings."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from starlette.requests import Request
from typer.testing import CliRunner

from farmcore import web
from farmcore.core.commands.migrate import alembic_config
from farmcore.core.manifest import Manifest
from farmcore.main import create_app
from farmcore.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _request(app) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


def test_health_reports_db_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["db"] == "ok"


def test_kernel_tables_and_postgis_exist(session) -> None:
    tables = set(
        session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
    )
    assert {"app_module", "clock_state", "alembic_version"} <= tables
    assert session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")).scalar()
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1
    assert session.execute(text("SELECT version_num FROM alembic_version")).scalar() == heads[0]


def test_base_template_has_navbar_and_static_replay_slot(settings, session_factory) -> None:
    app = create_app(
        settings,
        manifests=[Manifest(name="fields", version="0.1.0", nav=("Fields", "/fields/"))],
        session_factory=session_factory,
    )
    html = web.render(_request(app), "core/base.html").body.decode()
    assert '<nav id="navbar"' in html
    assert 'href="/fields/"' in html and "Fields" in html
    assert 'id="replay-control"' in html
    assert 'hx-get="/clock"' in html  # the slot loads the replay control
    assert "/static/core/vendor/tailwind-3.4.17.min.css" in html
    assert "/static/core/vendor/htmx-2.0.4.min.js" in html


def test_local_filter_shows_display_timezone(settings, session_factory) -> None:
    app = create_app(settings, manifests=[], session_factory=session_factory)
    tmpl = web.templates.env.from_string("{{ t|local }}")
    out = tmpl.render(request=_request(app), t=datetime(2024, 4, 10, 4, 0, tzinfo=UTC))
    assert out == "10 Apr 2024 06:00"


def test_root_redirects_to_health_without_nav(settings, session_factory) -> None:
    app = create_app(settings, manifests=[], session_factory=session_factory)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/health"


def test_root_redirects_to_first_nav_entry(settings, session_factory) -> None:
    manifests = [
        Manifest(name="a", version="0.1.0"),
        Manifest(name="b", version="0.1.0", nav=("B", "/b/")),
    ]
    with TestClient(
        create_app(settings, manifests=manifests, session_factory=session_factory)
    ) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        resp = c.get("/", follow_redirects=False)
    assert resp.headers["location"] == "/b/"


def test_create_app_loads_only_given_manifests(settings, session_factory) -> None:
    only = Manifest(name="solo", version="0.1.0")
    app = create_app(settings, manifests=[only], session_factory=session_factory)
    assert app.state.manifests == [only]


def test_create_app_loads_only_given_packages(settings, session_factory) -> None:
    bare = create_app(settings, app_packages=(), session_factory=session_factory)
    assert [m.name for m in bare.state.manifests] == ["core"]
    full = create_app(settings, session_factory=session_factory)
    assert_has_p0_apps([m.name for m in full.state.manifests])


# The core app set: other apps (`kpi`, ...) may be added, none of these may be missing.
P0_APPS = frozenset({"core", "fields", "stations", "operations", "advisor", "chat"})


def assert_has_p0_apps(names: list[str]) -> None:
    assert set(names) >= P0_APPS, sorted(P0_APPS - set(names))


def test_p0_app_check_tolerates_added_apps_and_fails_on_a_missing_one() -> None:
    fakes = [Manifest(name=n, version="0.1.0") for n in sorted(P0_APPS | {"kpi"})]
    assert_has_p0_apps([m.name for m in fakes])
    with pytest.raises(AssertionError):
        assert_has_p0_apps([m.name for m in fakes if m.name != "advisor"])


def _fake_app_package(tmp_path: Path, monkeypatch, app_name: str) -> str:
    """A throwaway importable package holding one app `app_name` with a `static/` file."""
    pkg = f"fake_apps_{uuid.uuid4().hex[:8]}"
    app_dir = tmp_path / pkg / app_name
    (app_dir / "static").mkdir(parents=True)
    (tmp_path / pkg / "__init__.py").write_text("")
    (app_dir / "__init__.py").write_text("")
    (app_dir / "manifest.py").write_text(
        "from farmcore.core.manifest import Manifest\n"
        f"MANIFEST = Manifest(name={app_name!r}, version='0.1.0', nav=('Fake', '/{app_name}/'))\n"
    )
    (app_dir / "static" / "probe.txt").write_text("fake static")
    monkeypatch.syspath_prepend(str(tmp_path))
    return pkg


def test_create_app_discovers_a_non_default_package_only(
    settings, session_factory, tmp_path, monkeypatch
) -> None:
    pkg = _fake_app_package(tmp_path, monkeypatch, "orchard")
    app = create_app(settings, app_packages=(pkg,), session_factory=session_factory)
    assert [m.name for m in app.state.manifests] == ["core", "orchard"]
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert c.get("/static/orchard/probe.txt").text == "fake static"
        assert c.get("/", follow_redirects=False).headers["location"] == "/orchard/"


def test_fake_app_reusing_a_real_name_keeps_its_own_static(
    settings, session_factory, tmp_path, monkeypatch
) -> None:
    from farmcore.core import registry

    pkg = _fake_app_package(tmp_path, monkeypatch, "fields")
    fake = registry.discover(packages=(pkg,))
    registry.discover()  # the real `fields` manifest is discovered afterwards
    app = create_app(settings, manifests=fake, session_factory=session_factory)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert c.get("/static/fields/probe.txt").text == "fake static"


def test_core_static_is_mounted(client: TestClient) -> None:
    assert client.get("/static/core/farmcore.css").status_code == 200


def test_cli_lists_migrate() -> None:
    from farmcore.cli import cli

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "migrate" in result.output


def test_settings_repr_hides_secrets() -> None:
    s = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:pw-secret-1@db:5432/x",
        telegram_bot_token="tg-secret-2",
        gemini_api_key="gm-secret-3",
        admin_token="adm-secret-4",
        vertex_access_token="vx-secret-5",
    )
    shown = repr(s) + str(s)
    for secret in ("pw-secret-1", "tg-secret-2", "gm-secret-3", "adm-secret-4", "vx-secret-5"):
        assert secret not in shown


def test_settings_defaults_and_empty_database_url(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    s = Settings(_env_file=None, database_url="")
    assert s.database_url == "postgresql+psycopg://farmcore:farmcore@localhost:55442/farmcore"
    assert make_url(s.test_database_url).database == "postgres"
    assert make_url(s.test_database_url).port == 55442
    assert s.display_tz == "Europe/Paris"
    assert s.clock_mode == "replay"
    assert s.replay_start == datetime(2024, 4, 1, 6, 0)
    assert s.default_farm_key == "chateau"
    assert s.vertex_model == "gemini-2.5-flash"


def test_get_settings_reads_dotenv_but_test_settings_do_not(tmp_path, monkeypatch) -> None:
    from farmcore.settings import get_settings

    monkeypatch.delenv("DISPLAY_TZ", raising=False)
    (tmp_path / ".env").write_text("DISPLAY_TZ=UTC\n")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().display_tz == "UTC"
        assert Settings(_env_file=None).display_tz == "Europe/Paris"
    finally:
        get_settings.cache_clear()


def _test_databases(admin_url) -> set[str]:
    eng = create_engine(admin_url)
    try:
        with eng.connect() as conn:
            return set(
                conn.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE 'farmcore_test_%'")
                ).scalars()
            )
    finally:
        eng.dispose()


@pytest.mark.slow
def test_session_database_is_dropped_after_the_session() -> None:
    admin_url = make_url(Settings(_env_file=None).test_database_url)
    name = f"farmcore_test_{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "FARMCORE_TEST_DB": name}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_smoke.py::test_health_reports_db_ok",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout
    assert name not in _test_databases(admin_url)


def test_settings_fixture_ignores_exported_time_and_clock_env(settings) -> None:
    # Run with e.g. DISPLAY_TZ=UTC CLOCK_MODE=live exported: the fixture must still pin these.
    assert settings.display_tz == "Europe/Paris"
    assert settings.clock_mode == "replay"
    assert settings.replay_start == datetime(2024, 4, 1, 6, 0)
    assert settings.default_farm_key == "chateau"
