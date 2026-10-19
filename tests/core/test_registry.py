"""App registry: discovery, dependency order, graph errors, app_module rows, seed and apps CLI."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.routing import Mount
from typer.testing import CliRunner

from farmcore.core import registry
from farmcore.core.manifest import Manifest
from farmcore.core.models.kernel import AppModule
from farmcore.main import create_app
from tests.core import fake_apps

FAKE = ("tests.core.fake_apps",)


def _m(name: str, *depends: str) -> Manifest:
    return Manifest(name=name, version="0.1.0", depends=depends)


def test_fake_chain_loads_dependencies_first_with_core_first() -> None:
    order = registry.resolve_order(registry.discover(packages=FAKE))
    assert [m.name for m in order] == ["core", "c", "b", "a"]


def test_ties_sort_alphabetically_whatever_the_input_order() -> None:
    given = [_m("zeta"), _m("mid", "zeta"), _m("alpha"), registry.CORE, _m("beta", "alpha")]
    order = registry.resolve_order(given)
    assert [m.name for m in order] == ["core", "alpha", "beta", "zeta", "mid"]
    assert registry.resolve_order(list(reversed(given))) == order


def test_cycle_is_refused_naming_only_the_apps_in_it() -> None:
    given = [registry.CORE, _m("x", "y"), _m("y", "z"), _m("z", "x"), _m("w", "x")]
    with pytest.raises(registry.RegistryError) as err:
        registry.resolve_order(given)
    msg = str(err.value)
    assert "cycle" in msg
    assert "'x' -> 'y' -> 'z' -> 'x'" in msg
    assert "'w'" not in msg


def test_missing_dependency_is_refused_naming_app_and_dependency() -> None:
    with pytest.raises(registry.RegistryError) as err:
        registry.resolve_order([registry.CORE, _m("orchard", "irrigation")])
    assert "orchard" in str(err.value) and "irrigation" in str(err.value)


def test_core_dependency_is_implicit_when_core_is_not_given() -> None:
    orchard = _m("orchard", "core")
    assert registry.resolve_order([orchard]) == [orchard]
    assert registry.active_manifests([orchard], {"orchard": "installed"}) == [orchard]


def test_create_app_with_only_a_real_app_manifest_loads_just_that_app(
    settings, session_factory
) -> None:
    from farmcore.apps.fields.manifest import MANIFEST as FIELDS

    app = create_app(settings, manifests=[FIELDS], session_factory=session_factory)
    assert app.state.manifests == [FIELDS]


def _rows(factory) -> dict[str, tuple[str, str, object]]:
    with factory() as s:
        rows = s.execute(select(AppModule)).scalars()
        return {r.name: (r.state, r.version, r.updated_at) for r in rows}


def test_app_module_rows_are_created_once_and_restart_changes_nothing(
    settings, committed_session_factory
) -> None:
    create_app(settings, app_packages=FAKE, session_factory=committed_session_factory)
    first = _rows(committed_session_factory)
    assert {n: r[:2] for n, r in first.items()} == {
        "core": ("installed", registry.CORE.version),
        "a": ("installed", "1.0.0"),
        "b": ("installed", "1.0.0"),
        "c": ("installed", "1.0.0"),
    }
    create_app(settings, app_packages=FAKE, session_factory=committed_session_factory)
    assert _rows(committed_session_factory) == first


def test_app_module_version_is_refreshed(settings, session_factory) -> None:
    create_app(settings, manifests=[_m("orchard")], session_factory=session_factory)
    bumped = replace(_m("orchard"), version="0.2.0")
    create_app(settings, manifests=[bumped], session_factory=session_factory)
    assert _rows(session_factory)["orchard"][:2] == ("installed", "0.2.0")


def _set_state(factory, name: str, state: str) -> None:
    with factory() as s:
        s.get(AppModule, name).state = state
        s.commit()


def test_uninstalled_app_is_skipped_at_next_start(settings, session_factory) -> None:
    app = create_app(settings, app_packages=FAKE, session_factory=session_factory)
    with TestClient(app, headers={"X-Admin-Token": settings.admin_token.get_secret_value()}) as c:
        assert c.get("/a/ping").json() == {"app": "a"}
    _set_state(session_factory, "a", "uninstalled")
    app = create_app(settings, app_packages=FAKE, session_factory=session_factory)
    assert [m.name for m in app.state.manifests] == ["core", "c", "b"]
    assert [m.name for m in app.state.all_manifests] == ["core", "c", "b", "a"]
    with TestClient(app, headers={"X-Admin-Token": settings.admin_token.get_secret_value()}) as c:
        assert c.get("/a/ping").status_code == 404
    assert _rows(session_factory)["a"][0] == "uninstalled"


def test_app_whose_dependency_is_uninstalled_is_not_loaded(settings, session_factory) -> None:
    create_app(settings, app_packages=FAKE, session_factory=session_factory)
    _set_state(session_factory, "b", "uninstalled")  # bypassing the 409 guard
    app = create_app(settings, app_packages=FAKE, session_factory=session_factory)
    assert [m.name for m in app.state.manifests] == ["core", "c"]
    assert _rows(session_factory)["a"][0] == "installed"


def test_models_are_imported_before_mappers_are_configured(
    settings, session_factory, monkeypatch
) -> None:
    seen: list[bool] = []
    real = registry.configure_mappers
    monkeypatch.delitem(sys.modules, "tests.core.fake_apps.c.models", raising=False)

    def spy() -> None:
        seen.append("tests.core.fake_apps.c.models" in sys.modules)
        real()

    monkeypatch.setattr(registry, "configure_mappers", spy)
    create_app(settings, app_packages=FAKE, session_factory=session_factory)
    assert seen == [True]


def test_core_static_is_mounted_exactly_once(settings, session_factory) -> None:
    app = create_app(settings, app_packages=FAKE, session_factory=session_factory)
    mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == "/static/core"]
    assert len(mounts) == 1


def test_create_app_fails_at_start_on_a_broken_graph(settings, session_factory) -> None:
    with pytest.raises(registry.RegistryError, match="p.*q|q.*p"):
        create_app(
            settings, manifests=[_m("p", "q"), _m("q", "p")], session_factory=session_factory
        )
    with pytest.raises(registry.RegistryError, match="lonely.*ghost"):
        create_app(settings, manifests=[_m("lonely", "ghost")], session_factory=session_factory)


@pytest.fixture
def cli_on_test_db(settings, committed_session_factory, monkeypatch):
    """`farmcore` CLI bound to the test database and the fake apps; core's seed recorded."""
    from farmcore.core.commands import apps as apps_cmd
    from farmcore.core.commands import seed as seed_cmd

    for module in (apps_cmd, seed_cmd):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(module, "APP_PACKAGES", FAKE)
    monkeypatch.setattr("farmcore.core.seed.seed", fake_apps.make_seed("core"))
    fake_apps.CALLS.clear()
    from farmcore.cli import cli

    return lambda *args: CliRunner().invoke(cli, list(args))


def test_seed_runs_installed_seeds_in_load_order_with_the_scene(cli_on_test_db) -> None:
    result = cli_on_test_db("seed", "--scene", "B")
    assert result.exit_code == 0, result.output
    assert fake_apps.CALLS == [("core", "B"), ("c", "B"), ("b", "B"), ("a", "B")]
    lines = result.output.splitlines()
    assert lines[:4] == ["core: core_rows=1", "c: c_rows=2", "b: b_rows=3", "a: a_rows=4"]


def test_seed_without_scene_skips_uninstalled_apps(
    cli_on_test_db, settings, committed_session_factory
) -> None:
    create_app(settings, app_packages=FAKE, session_factory=committed_session_factory)
    _set_state(committed_session_factory, "a", "uninstalled")
    result = cli_on_test_db("seed")
    assert result.exit_code == 0, result.output
    assert fake_apps.CALLS == [("core", None), ("c", None), ("b", None)]


def test_apps_command_prints_load_order_and_states(
    cli_on_test_db, settings, committed_session_factory
) -> None:
    create_app(settings, app_packages=FAKE, session_factory=committed_session_factory)
    _set_state(committed_session_factory, "a", "uninstalled")
    result = cli_on_test_db("apps")
    assert result.exit_code == 0, result.output
    rows = [line.split() for line in result.output.splitlines()]
    assert [r[:4] for r in rows] == [
        ["1", "core", registry.CORE.version, "installed"],
        ["2", "c", "1.0.0", "installed"],
        ["3", "b", "1.0.0", "installed"],
        ["4", "a", "1.0.0", "uninstalled"],
    ]


def test_seed_rolls_back_every_app_when_a_later_seed_fails(
    cli_on_test_db, committed_session_factory, monkeypatch
) -> None:
    def core_seed(session, *, scene, data_dir):
        session.add(AppModule(name="seed-probe", version="0"))
        session.flush()
        return {"probe": 1}

    def failing_seed(session, *, scene, data_dir):
        raise RuntimeError("seed b broke")

    monkeypatch.setattr("farmcore.core.seed.seed", core_seed)
    monkeypatch.setattr("tests.core.fake_apps.b.manifest.seed", failing_seed)
    result = cli_on_test_db("seed")
    assert result.exit_code != 0
    with committed_session_factory() as s:
        assert s.get(AppModule, "seed-probe") is None


def test_visible_from_states_hides_only_uninstalled_optional_apps() -> None:
    core = registry.CORE
    plain = Manifest(name="plain", version="0.1.0")
    off = Manifest(name="off", version="0.1.0")
    needed = Manifest(name="needed", version="0.1.0", required=True)
    states = {"off": registry.UNINSTALLED, "needed": registry.UNINSTALLED, "core": "uninstalled"}

    visible = registry.visible_from_states([core, plain, off, needed], states)

    assert [m.name for m in visible] == ["core", "plain", "needed"]
