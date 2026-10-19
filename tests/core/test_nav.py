"""The shell's navigation comes from the loaded manifests, grouped Farmer / Expert / Admin."""

from __future__ import annotations

from pydantic import SecretStr
from starlette.requests import Request

from farmcore import web
from farmcore.core.manifest import Manifest, NavItem
from farmcore.core.nav import active_path, build_nav
from farmcore.main import create_app

TOKEN = "nav-test-token"

FAKES = [
    Manifest(name="map", version="0.1.0", nav=("Map", "/map/"), nav_group="farmer", nav_order=20),
    Manifest(
        name="ops",
        version="0.1.0",
        nav=("Diary", "/ops/"),
        nav_group="farmer",
        nav_order=30,
        extra_nav=(NavItem("Register", "/ops/register", "farmer", 40),),
    ),
    Manifest(
        name="home", version="0.1.0", nav=("Today", "/home/"), nav_group="farmer", nav_order=10
    ),
    Manifest(name="kpi", version="0.1.0", nav=("KPIs", "/kpi/"), nav_order=30),
    Manifest(name="st", version="0.1.0", nav=("Stations", "/st/"), nav_order=20),
    Manifest(name="silent", version="0.1.0"),
]


def _labels(items: list[NavItem]) -> list[str]:
    return [i.label for i in items]


def test_farmer_group_is_ordered_and_includes_extra_nav() -> None:
    nav = build_nav(FAKES, is_admin=False)
    assert _labels(nav["farmer"]) == ["Today", "Map", "Diary", "Register"]
    assert _labels(nav["more"]) == ["Stations", "KPIs"]


def test_all_apps_ends_the_apps_group_for_everyone() -> None:
    # The Apps page is read-only for non-admins, so its link is no longer admin-only.
    w = Manifest(name="w", version="0.1.0", nav=("W", "/w/"), nav_group="apps", nav_order=40)
    fakes = [*FAKES, w]
    for admin in (False, True):
        nav = build_nav(fakes, is_admin=admin)
        assert [(i.label, i.path) for i in nav["apps"]] == [("W", "/w/"), ("All apps", "/apps")]
        assert nav["admin"] == []


def test_admin_items_only_for_admins() -> None:
    admin_only = Manifest(name="x", version="0.1.0", nav=("X", "/x/"), nav_group="admin")
    assert build_nav([admin_only], is_admin=False)["admin"] == []
    assert _labels(build_nav([admin_only], is_admin=True)["admin"]) == ["X"]


def test_ties_keep_load_order() -> None:
    a = Manifest(name="a", version="0.1.0", nav=("A", "/a/"))
    b = Manifest(name="b", version="0.1.0", nav=("B", "/b/"))
    assert _labels(build_nav([b, a], is_admin=False)["more"]) == ["B", "A"]


def test_active_path_is_the_longest_matching_prefix() -> None:
    items = build_nav(FAKES, is_admin=False)["farmer"]
    assert active_path(items, "/ops/register") == "/ops/register"
    assert active_path(items, "/ops/parcels/3") == "/ops/"
    assert active_path(items, "/elsewhere") is None


def _app(settings, session_factory):
    from farmcore.core.seed import ensure_default_farm

    with session_factory() as session:
        farm = ensure_default_farm(session, settings)
        farm_id = farm.id
        session.commit()
    return create_app(
        settings.model_copy(
            update={"admin_token": SecretStr(TOKEN), "demo_mode": True, "demo_farm_ids": [farm_id]}
        ),
        session_factory=session_factory,
    )


def _page(app, path: str, *, admin: bool = False) -> str:
    headers = [(b"x-admin-token", TOKEN.encode())] if admin else []
    request = Request(
        {"type": "http", "method": "GET", "path": path, "headers": headers, "app": app}
    )
    return web.render(request, "core/base.html").body.decode()


def test_shell_renders_bottom_nav_more_and_hides_admin(settings, session_factory) -> None:
    app = _app(settings, session_factory)
    html = _page(app, "/fields/")
    assert '<nav id="navbar"' in html
    bottom = html.split('id="bottom-nav"', 1)[1].split("</nav>", 1)[0]
    for label in ("Home", "Map", "Plan", "Activity", "More"):
        assert label in bottom
    assert "Weather" not in bottom
    more = html.split('id="more-menu"', 1)[1].split("</dialog>", 1)[0]
    apps, expert = more.split("Records", 1)
    for label in ("Crop protection", "Weather", "Season &amp; impact", "Sharing"):
        assert label in apps
    for label in ("Diary", "AI log"):
        assert label in expert
    side = html.split('id="nav-apps"', 1)[1].split("</div>", 1)[0]
    assert "Crop protection" in side and "Diary" not in side
    assert 'href="/apps"' in side  # everyone can see the Apps page
    assert 'href="/apps"' in apps


def test_current_page_is_marked(settings, session_factory) -> None:
    html = _page(_app(settings, session_factory), "/operations/register")
    assert 'href="/operations/register" aria-current="page"' in html
    assert 'href="/operations/" aria-current="page"' not in html
