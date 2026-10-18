"""`/apps`: list apps in load order, uninstall and install (applied at the next start)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from farmcore.core.manifest import Manifest
from farmcore.main import create_app

FAKE = ("tests.core.fake_apps",)


@pytest.fixture
def fake_client(settings, session_factory) -> Iterator[TestClient]:
    app = create_app(settings, app_packages=FAKE, session_factory=session_factory)
    with TestClient(app) as c:
        yield c


def _restart(settings, session_factory) -> TestClient:
    return TestClient(create_app(settings, app_packages=FAKE, session_factory=session_factory))


def test_apps_json_lists_apps_in_load_order_with_maturity(fake_client: TestClient) -> None:
    apps = fake_client.get("/apps.json").json()
    assert [(a["name"], a["maturity"], a["state"]) for a in apps] == [
        ("core", "alpha", "installed"),
        ("c", "stable", "installed"),
        ("b", "alpha", "installed"),
        ("a", "beta", "installed"),
    ]
    assert apps[3]["depends"] == ["b"]
    assert apps[3]["version"] == "1.0.0"
    assert all(a["loaded"] for a in apps)


# The core apps, in load order. Other apps (`kpi`, ...) may load anywhere their `depends`
# allow; the core apps keep this relative order and none may be missing.
P0_ORDER = ("core", "fields", "operations", "stations", "advisor", "chat")


def assert_graph_order(apps: list[dict]) -> None:
    """Every core app is listed, in `P0_ORDER`; every app comes after each of its `depends`."""
    names = [a["name"] for a in apps]
    assert [n for n in names if n in P0_ORDER] == list(P0_ORDER), names
    for i, a in enumerate(apps):
        assert set(a["depends"]) <= set(names[:i]), (a["name"], names)


def test_real_apps_load_in_graph_order(client: TestClient) -> None:
    assert_graph_order(client.get("/apps.json").json())


def _listing(*manifests: Manifest) -> list[dict]:
    return [{"name": m.name, "depends": list(m.depends)} for m in manifests]


_P0 = {
    "core": (),
    "fields": ("core",),
    "operations": ("core", "fields"),
    "stations": ("core", "fields"),
    "advisor": ("fields", "stations", "operations"),
    "chat": ("fields", "operations", "advisor"),
}


def _fake(name: str, depends: tuple[str, ...] | None = None) -> Manifest:
    return Manifest(name=name, version="0.1.0", depends=_P0[name] if depends is None else depends)


def test_graph_order_tolerates_an_added_app_after_its_depends() -> None:
    apps = [_fake(n) for n in P0_ORDER[:5]] + [_fake("chat")]
    apps.insert(5, Manifest(name="kpi", version="0.1.0", depends=("advisor", "operations")))
    assert_graph_order(_listing(*apps))


def test_graph_order_fails_when_a_p0_app_is_missing_or_out_of_order() -> None:
    missing = [_fake(n) for n in P0_ORDER if n != "advisor"]
    with pytest.raises(AssertionError):
        assert_graph_order(_listing(*missing))
    swapped = [_fake(n) for n in ("core", "fields", "stations", "operations")]
    swapped += [_fake(n) for n in P0_ORDER[4:]]
    with pytest.raises(AssertionError):
        assert_graph_order(_listing(*swapped))


def test_graph_order_fails_when_an_added_app_precedes_its_depends() -> None:
    apps = [Manifest(name="kpi", version="0.1.0", depends=("advisor",))]
    apps += [_fake(n) for n in P0_ORDER]
    with pytest.raises(AssertionError):
        assert_graph_order(_listing(*apps))


def test_uninstall_is_refused_while_an_installed_app_depends_on_it(
    fake_client: TestClient,
) -> None:
    resp = fake_client.post("/apps/b/uninstall")
    assert resp.status_code == 409
    assert resp.json()["dependants"] == ["a"]
    assert resp.json()["detail"] == "A needs B. Switch off A first."
    assert fake_client.get("/apps.json").json()[2]["state"] == "installed"


def test_core_cannot_be_uninstalled(fake_client: TestClient) -> None:
    assert fake_client.post("/apps/core/uninstall").status_code == 409


def test_unknown_app_is_404(fake_client: TestClient) -> None:
    assert fake_client.post("/apps/ghost/uninstall").status_code == 404
    assert fake_client.post("/apps/ghost/install").status_code == 404


def test_uninstalling_a_leaf_unmounts_it_at_the_next_start(
    fake_client: TestClient, settings, session_factory
) -> None:
    resp = fake_client.post("/apps/a/uninstall")
    assert resp.status_code == 200
    assert resp.json()["state"] == "uninstalled"
    # Still mounted until the next start; the listing shows the new state.
    assert fake_client.get("/a/ping").status_code == 200
    a = fake_client.get("/apps.json").json()[3]
    assert (a["state"], a["loaded"]) == ("uninstalled", True)
    with _restart(settings, session_factory) as c:
        assert c.get("/a/ping").status_code == 404
        a = c.get("/apps.json").json()[3]
        assert (a["name"], a["state"], a["loaded"]) == ("a", "uninstalled", False)


def test_install_is_refused_while_a_dependency_is_uninstalled(fake_client: TestClient) -> None:
    assert fake_client.post("/apps/a/uninstall").status_code == 200
    assert fake_client.post("/apps/b/uninstall").status_code == 200
    resp = fake_client.post("/apps/a/install")
    assert resp.status_code == 409
    assert resp.json()["uninstalled_dependencies"] == ["b"]
    assert fake_client.post("/apps/b/install").json()["state"] == "installed"
    assert fake_client.post("/apps/a/install").json()["state"] == "installed"


def test_install_is_refused_while_a_dependency_will_not_load(
    fake_client: TestClient, session_factory
) -> None:
    from farmcore.core.models.kernel import AppModule

    assert fake_client.post("/apps/a/uninstall").status_code == 200
    with session_factory() as s:  # b stays installed but cannot load without c
        s.get(AppModule, "c").state = "uninstalled"
        s.commit()
    resp = fake_client.post("/apps/a/install")
    assert resp.status_code == 409
    assert resp.json()["uninstalled_dependencies"] == ["b"]


def test_apps_page_expert_details_show_load_order_versions_and_badges(
    fake_client: TestClient,
) -> None:
    fake_client.cookies.set("expert", "1")
    html = fake_client.get("/apps").text.split("Technical details", 1)[1]
    assert html.index(">core<") < html.index(">c<") < html.index(">b<") < html.index(">a<")
    assert "1.0.0" in html
    assert 'data-maturity="stable"' in html and 'data-maturity="beta"' in html


def test_apps_page_switches_every_app_but_core(fake_client: TestClient) -> None:
    html = fake_client.get("/apps").text
    assert 'action="/apps/a/uninstall"' in html
    assert 'action="/apps/core/uninstall"' not in html


def test_html_form_post_redirects_back_or_shows_the_refusal(fake_client: TestClient) -> None:
    html = {"accept": "text/html"}
    resp = fake_client.post("/apps/a/uninstall", headers=html, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/apps"
    followed = fake_client.post("/apps/b/install", headers=html)  # already installed: no-op
    assert followed.status_code == 200 and followed.url.path == "/apps"
    refused = fake_client.post("/apps/c/uninstall", headers=html)
    assert refused.status_code == 409
    assert 'role="alert"' in refused.text and "B needs C." in refused.text


def test_nav_hides_apps_that_are_not_loaded(
    fake_client: TestClient, settings, session_factory
) -> None:
    assert 'href="/a/ping"' in fake_client.get("/apps").text
    fake_client.post("/apps/a/uninstall")
    with _restart(settings, session_factory) as c:
        assert 'href="/a/ping"' not in c.get("/apps").text
