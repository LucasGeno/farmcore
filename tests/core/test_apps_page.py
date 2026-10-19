"""The Apps page as a tile grid in farmer words, required apps, and live UI switching."""

from __future__ import annotations

import html as htmllib
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request

from farmcore.apps.today.manifest import MANIFEST as TODAY
from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.manifest import Manifest, Widget
from farmcore.core.registry import visible_manifests
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app

FAKE = ("tests.core.fake_apps",)


def _tile(page: str, name: str) -> str:
    return page.split(f'data-app="{name}"', 1)[1].split("</article>", 1)[0]


def _main(page: str) -> str:
    return page.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _side_nav(page: str) -> str:
    return page.split('<nav id="navbar"', 1)[1].split("</nav>", 1)[0]


@pytest.fixture
def fake_client(settings, session_factory) -> Iterator[TestClient]:
    with TestClient(create_app(settings, app_packages=FAKE, session_factory=session_factory)) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        yield c


# --- the grid -------------------------------------------------------------------------------


def test_tiles_are_grouped_by_category_essentials_first_then_alphabetical(client) -> None:
    page = client.get("/apps").text
    categories = [htmllib.unescape(c) for c in re.findall(r'data-category="([^"]+)"', page)]
    assert categories[0] == "Essentials"
    assert categories[1:] == sorted(categories[1:])
    assert {"Crop protection", "Weather", "Insights"} <= set(categories)
    assert 'data-app="core"' not in page
    essentials = page.split('data-category="Essentials"', 1)[1].split("</section>", 1)[0]
    assert {"Home", "Map &amp; parcels", "Records"} <= set(
        re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h3[^>]*>(.*?)</h3>", essentials)
    )


def test_page_header_and_tile_parts(client) -> None:
    page = client.get("/apps").text
    assert "Switch on what your farm needs. Everything is free and open source." in page
    advisor = _tile(page, "advisor")
    assert "Crop protection" in advisor
    assert "Tells you which parcels to spray, when, and why." in advisor
    assert "Needs Weather" in advisor
    assert "Needs Parcels" not in advisor  # required apps are always on: not worth saying
    assert "<svg" in advisor


def test_every_real_summary_is_one_short_sentence(client) -> None:
    for app in client.get("/apps.json").json():
        if app["name"] != "core":
            assert 0 < len(app["summary"]) <= 80, app["name"]
            assert app["summary"].endswith("."), app["name"]


def test_listing_json_carries_title_category_required(client) -> None:
    apps = {a["name"]: a for a in client.get("/apps.json").json()}
    assert (apps["advisor"]["title"], apps["advisor"]["category"]) == (
        "Crop protection",
        "Crop protection",
    )
    assert apps["fields"]["required"] is True and apps["fields"]["removable"] is False
    assert apps["share"]["required"] is False and apps["share"]["removable"] is True
    assert apps["core"]["required"] is True


# --- status and switches --------------------------------------------------------------------


def test_required_app_is_always_on_and_refuses_uninstall(client) -> None:
    fields = _tile(client.get("/apps").text, "fields")
    assert "Always on" in fields and "<form" not in fields
    resp = client.post("/apps/fields/uninstall")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Map & parcels is always on."
    assert client.post("/apps/today/uninstall").status_code == 409


def test_installed_app_shows_on_with_a_switch_off(client) -> None:
    share = _tile(client.get("/apps").text, "share")
    assert ">On<" in share
    assert 'action="/apps/share/uninstall"' in share


def test_uninstall_with_dependants_is_refused_in_farmer_words(client, fake_client) -> None:
    resp = client.post("/apps/stations/uninstall")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Crop protection" in detail and "need Weather." in detail
    assert detail.endswith("Switch them off first.")
    assert "advisor" in resp.json()["dependants"]
    one = fake_client.post("/apps/c/uninstall").json()
    assert one["detail"] == "B needs C. Switch off B first."


def test_html_refusal_shows_the_farmer_sentence(client) -> None:
    resp = client.post("/apps/stations/uninstall", headers={"accept": "text/html"})
    assert resp.status_code == 409
    alert = resp.text.split('role="alert"', 1)[1].split("</p>", 1)[0]
    assert "need Weather" in alert


def test_install_refusal_names_the_missing_app(fake_client) -> None:
    assert fake_client.post("/apps/a/uninstall").status_code == 200
    assert fake_client.post("/apps/b/uninstall").status_code == 200
    resp = fake_client.post("/apps/a/install")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "A needs B. Switch on B first."


def test_switched_off_app_shows_off_with_a_switch_on(fake_client) -> None:
    fake_client.post("/apps/a/uninstall")
    a = _tile(fake_client.get("/apps").text, "a")
    assert ">Off<" in a and 'action="/apps/a/install"' in a


def test_app_not_loaded_at_start_says_after_the_next_restart(
    fake_client, settings, session_factory
) -> None:
    fake_client.post("/apps/a/uninstall")
    with TestClient(create_app(settings, app_packages=FAKE, session_factory=session_factory)) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert c.post("/apps/a/install").status_code == 200
        a = _tile(c.get("/apps").text, "a")
    assert "On after the next restart" in a


def test_non_admin_sees_the_page_read_only(settings, session_factory) -> None:
    with session_factory() as session:
        farm = ensure_default_farm(session, settings)
        farm_id = farm.id
        session.commit()
    app = create_app(
        settings.model_copy(
            update={"admin_token": SecretStr("tok"), "demo_mode": True, "demo_farm_ids": [farm_id]}
        ),
        manifests=[CORE, Manifest(name="a", version="0.1.0", title="Alpha")],
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        page = c.get("/apps").text
    assert "Only the farm admin can switch apps." in page
    assert "Alpha" in page and "<form" not in _main(page)


def test_technical_details_only_in_expert_view(fake_client) -> None:
    plain = _main(fake_client.get("/apps").text)
    assert "Technical details" not in plain and "1.0.0" not in plain
    fake_client.cookies.set("expert", "1")
    expert = _main(fake_client.get("/apps").text)
    details = expert.split("Technical details", 1)[1]
    assert "<details" in expert and "<details open" not in expert
    assert "1.0.0" in details and 'data-maturity="stable"' in details


# --- live switching -------------------------------------------------------------------------

WIDGETY = Manifest(
    name="w",
    version="0.1.0",
    title="Widgety",
    nav=("Widgety", "/w/"),
    nav_group="apps",
    widgets=(Widget("/w/today", 10),),
)


def test_switching_off_hides_nav_and_today_widget_at_once(settings, session_factory) -> None:
    app = create_app(settings, manifests=[CORE, TODAY, WIDGETY], session_factory=session_factory)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        page = c.get("/today/").text
        assert 'href="/w/"' in _side_nav(page) and 'hx-get="/w/today"' in page
        assert c.post("/apps/w/uninstall").status_code == 200
        page = c.get("/today/").text
        assert 'href="/w/"' not in page and 'hx-get="/w/today"' not in page
        assert c.post("/apps/w/install").status_code == 200
        page = c.get("/today/").text
        assert 'href="/w/"' in _side_nav(page) and 'hx-get="/w/today"' in page


def test_switching_off_sharing_hides_it_from_the_real_nav(client) -> None:
    assert "Sharing" in _side_nav(client.get("/today/").text)
    assert client.post("/apps/share/uninstall").status_code == 200
    page = client.get("/today/").text
    assert "Sharing" not in _side_nav(page) and 'href="/share/"' not in page
    assert client.post("/apps/share/install").status_code == 200
    assert "Sharing" in _side_nav(client.get("/today/").text)


def test_visible_manifests_without_a_database_is_every_loaded_app() -> None:
    class State:
        manifests = [CORE, WIDGETY]

    class App:
        state = State()

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": App()})
    assert visible_manifests(request) == [CORE, WIDGETY]


def test_visible_manifests_skips_uninstalled_and_reads_once_per_request(
    settings, session_factory
) -> None:
    app = create_app(settings, manifests=[CORE, TODAY, WIDGETY], session_factory=session_factory)
    from farmcore.core.models.kernel import AppModule

    with session_factory() as s:
        s.get(AppModule, "w").state = "uninstalled"
        s.commit()
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})
    assert [m.name for m in visible_manifests(request)] == ["core", "today"]
    with session_factory() as s:
        s.get(AppModule, "w").state = "installed"
        s.commit()
    assert [m.name for m in visible_manifests(request)] == ["core", "today"]  # cached
