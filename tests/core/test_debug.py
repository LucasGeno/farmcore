"""Debug mode: the `debug_enabled` rule, the `/debug/drawer` slot and its admin gating.

The rule: `is_admin(request) and (q == "1" or (q != "0" and cookie == "1"))` with
`q = ?debug=` and cookie `farmcore_debug`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request

from farmcore.core.access import ensure_owner_grant
from farmcore.core.clock import clock_row
from farmcore.core.debug import debug_enabled
from farmcore.core.manifest import Manifest, Widget
from farmcore.core.models.canonical import AccessLog, Farm
from farmcore.core.models.kernel import AppModule
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app

TOKEN = "tok-debug"


def _request(settings, *, query: str = "", cookies: dict[str, str] | None = None) -> Request:
    cookie = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    headers = [(b"cookie", cookie.encode())] if cookie else []
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    return Request({"type": "http", "app": app, "query_string": query.encode(), "headers": headers})


ADMIN = {"admin_token": TOKEN}


@pytest.mark.parametrize(
    ("admin", "query", "debug_cookie", "expected"),
    [
        # admin: ?debug=1 on, ?debug=0 off, cookie alone on, nothing off
        (True, "debug=1", None, True),
        (True, "debug=1", "0", True),
        (True, "debug=0", "1", False),
        (True, "debug=0", None, False),
        (True, "", "1", True),
        (True, "", None, False),
        (True, "", "0", False),
        (True, "debug=yes", "1", True),
        (True, "debug=yes", None, False),
        # non-admin: never, whatever the query or cookie
        (False, "debug=1", None, False),
        (False, "debug=1", "1", False),
        (False, "", "1", False),
    ],
)
def test_debug_enabled_follows_the_rule(settings, admin, query, debug_cookie, expected) -> None:
    configured = settings.model_copy(update={"admin_token": SecretStr(TOKEN)})
    cookies = dict(ADMIN) if admin else {}
    if debug_cookie is not None:
        cookies["farmcore_debug"] = debug_cookie
    assert debug_enabled(_request(configured, query=query, cookies=cookies)) is expected


def test_debug_enabled_without_an_admin_token_fails_closed(settings) -> None:
    unconfigured = settings.model_copy(update={"admin_token": None})
    assert debug_enabled(_request(unconfigured, query="debug=1")) is False


# --- GET /debug/drawer ----------------------------------------------------------------------


def _client(settings, session_factory) -> TestClient:
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr(TOKEN)}),
        session_factory=session_factory,
    )
    return TestClient(app)


@pytest.fixture
def farm(session, settings):
    farm = ensure_default_farm(session, settings)
    row = clock_row(session, settings)
    row.job_runs = {
        "last_run": {"stations.ingest_meteofrance": "2024-04-10T04:00:00+00:00"},
        "last_step": {
            "to": "2024-04-10T04:00:00+00:00",
            "runs": [{"name": "advisor.evaluate", "ok": True, "error": None, "ms": 12}],
        },
    }
    session.commit()
    return farm


def test_anonymous_drawer_is_denied_and_sets_no_cookie(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        resp = c.get("/debug/drawer", params={"path": "/fields/", "debug": "1"})
    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers


def test_admin_debug_1_sets_the_cookie_and_shows_the_drawer(
    settings, session_factory, session, farm
) -> None:
    grant = ensure_owner_grant(session, farm)
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        resp = c.get("/debug/drawer", params={"path": "/fields/", "debug": "1"})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("farmcore_debug=1;")
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert "max-age" not in cookie.lower() and "expires" not in cookie.lower()  # session-length
    body = resp.text
    assert 'id="debug-drawer"' in body
    assert f'data-farm-id="{farm.id}"' in body
    assert "farmcore-demo:chateau" in body  # farm key
    assert f'data-grant-id="{grant.id}"' in body
    assert 'data-scopes="*"' in body  # the owner grant
    assert 'data-clock-mode="replay"' in body
    assert "stations.ingest_meteofrance" in body and "advisor.evaluate" in body


def test_admin_debug_0_clears_the_cookie(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        c.cookies.set("farmcore_debug", "1")
        resp = c.get("/debug/drawer", params={"path": "/fields/", "debug": "0"})
    cookie = resp.headers["set-cookie"]
    assert cookie.split(";")[0] in ('farmcore_debug=""', "farmcore_debug=")
    assert "max-age=0" in cookie.lower()
    assert 'id="debug-drawer"' not in resp.text
    assert 'href="/fields/?debug=1"' in resp.text  # the small "Debug" link


def test_admin_with_debug_off_gets_only_the_debug_link(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        resp = c.get("/debug/drawer", params={"path": "/operations/register"})
    assert "set-cookie" not in resp.headers
    assert 'href="/operations/register?debug=1"' in resp.text
    assert 'id="debug-drawer"' not in resp.text


def test_debug_link_clears_the_phone_bottom_nav(settings, session_factory, farm) -> None:
    """The off-state link renders after <main>: on a long phone page it must not end up under
    the fixed bottom nav (QA, 375x812: rect 752-812, not tappable)."""
    import re
    from pathlib import Path

    import farmcore.core

    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        resp = c.get("/debug/drawer", params={"path": "/today/"})
    slot = re.search(r'<div id="debug-slot" class="([^"]*)"', resp.text)
    assert slot is not None and "fc-debug-off" in slot.group(1).split()
    css = (Path(farmcore.core.__file__).parent / "static" / "farmcore.css").read_text()
    rule = re.search(r"\.fc-debug-off\s*\{([^}]*)\}", css)
    assert rule is not None and "padding-bottom" in rule.group(1)
    assert "--fc-bottom-nav-h" in rule.group(1)


def test_admin_cookie_keeps_debug_on_without_the_query(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        c.cookies.set("farmcore_debug", "1")
        resp = c.get("/debug/drawer", params={"path": "/fields/"})
    assert 'id="debug-drawer"' in resp.text


def test_drawer_path_must_be_local(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        resp = c.get("/debug/drawer", params={"path": "//evil.example/x"})
    assert "evil.example" not in resp.text
    assert 'href="/?debug=1"' in resp.text


def test_every_page_carries_the_slot_with_its_path_and_debug_value(
    settings, session_factory, farm
) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        on = c.get("/apps", params={"debug": "1"}).text
        plain = c.get("/apps").text
    assert 'hx-get="/debug/drawer?path=/apps&amp;debug=1"' in on
    assert 'hx-get="/debug/drawer?path=/apps"' in plain
    assert 'id="debug-drawer"' not in on  # the drawer itself only comes from /debug/drawer


# --- Manifests and app panels (stub manifests) ----------------------------------------------


def _stub(name: str, **fields) -> Manifest:
    return Manifest(name=name, version="9.9.9", maturity="beta", **fields)


STUBS = [
    _stub("alpha", nav_group="farmer", widgets=(Widget("/alpha/today", 10),), tools=("t1", "t2")),
    _stub("parcelapp", debug_panels=("/parcelapp/debug/parcels/{id}",)),
    _stub("globalapp", debug_panels=("/globalapp/debug/summary",)),
]


def _drawer(settings, session_factory, path: str) -> str:
    with _client(settings, session_factory) as c:
        c.app.state.manifests = [*c.app.state.manifests, *STUBS]
        c.cookies.set("admin_token", TOKEN)
        return c.get("/debug/drawer", params={"path": path, "debug": "1"}).text


def test_drawer_lists_every_loaded_manifest(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        names = [m.name for m in c.app.state.manifests]
    body = _drawer(settings, session_factory, "/fields/")
    for name in [*names, "alpha", "parcelapp", "globalapp"]:
        assert f'data-app="{name}"' in body
    assert "core" in names and "advisor" in names
    # alpha: nav group farmer, 1 today fragment, 0 panels, 2 tools
    assert (
        '<tr data-app="alpha"><td class="pr-3">alpha</td><td class="pr-3">9.9.9</td>'
        '<td class="pr-3">beta</td><td class="pr-3">farmer</td><td class="pr-3">1</td>'
        '<td class="pr-3">0</td><td>2</td></tr>'
    ) in body


def test_id_panels_load_only_on_their_own_apps_parcel_pages(
    settings, session_factory, farm
) -> None:
    # An app's `{id}` panels (and `{id}` raw links) load on that app's `/<app>/…/parcels/{id}`
    # pages only; panels without `{id}` load everywhere.
    own = _drawer(settings, session_factory, "/parcelapp/parcels/42")
    assert 'hx-get="/parcelapp/debug/parcels/42"' in own
    assert 'hx-get="/globalapp/debug/summary"' in own
    assert 'data-parcel-id="42"' in own
    assert "/advisor/parcels/42.json" not in own

    advisor = _drawer(settings, session_factory, "/advisor/parcels/42")
    assert 'href="/advisor/parcels/42.json"' in advisor
    assert "/parcelapp/debug/parcels/" not in advisor
    assert 'hx-get="/globalapp/debug/summary"' in advisor

    # The operations timeline is a `/parcels/{id}` page too, but of another app.
    timeline = _drawer(settings, session_factory, "/operations/parcels/42")
    assert 'data-parcel-id="42"' in timeline
    assert "/parcelapp/debug/parcels/" not in timeline
    assert "/advisor/parcels/42.json" not in timeline
    assert "Advice JSON" not in timeline
    assert 'hx-get="/globalapp/debug/summary"' in timeline

    elsewhere = _drawer(settings, session_factory, "/fields/")
    assert "/parcelapp/debug/parcels/" not in elsewhere
    assert "{id}" not in elsewhere
    assert 'hx-get="/globalapp/debug/summary"' in elsewhere
    assert "/advisor/parcels/" not in elsewhere
    assert 'href="/fields/parcels.geojson"' in elsewhere


def test_real_advisor_manifest_loads_its_panel_on_advisor_parcel_pages(
    settings, session_factory, farm
) -> None:
    # The real manifests, no stubs: the advisor declares its `{id}` panel in `debug_panels`.
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        card = c.get("/debug/drawer", params={"path": "/advisor/parcels/7", "debug": "1"}).text
        timeline = c.get(
            "/debug/drawer", params={"path": "/operations/parcels/7", "debug": "1"}
        ).text
    assert 'hx-get="/advisor/debug/parcels/7"' in card
    assert "/advisor/debug/parcels/" not in timeline


# --- The page query and the page farm -------------------------------------------------------


def test_slot_forwards_the_page_query_without_debug(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        html = c.get("/apps", params={"farm": "5", "x": "a b", "debug": "1"}).text
    assert 'hx-get="/debug/drawer?path=/apps%3Ffarm%3D5%26x%3Da%2Bb&amp;debug=1"' in html


@pytest.fixture
def other_farm(session, farm):
    other = Farm(name="Second test farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    return other


def test_drawer_reads_the_page_farm(settings, session_factory, farm, other_farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        page = f"/fields/?farm={other_farm.id}"
        resp = c.get("/debug/drawer", params={"path": page, "debug": "1"})
        default = c.get("/debug/drawer", params={"path": "/fields/", "debug": "1"})
        bogus = c.get("/debug/drawer", params={"path": "/fields/?farm=999999", "debug": "1"})
    assert f'data-farm-id="{other_farm.id}"' in resp.text
    assert f'data-farm-id="{farm.id}"' in default.text
    assert f'data-farm-id="{farm.id}"' in bogus.text  # not a farm: the drawer's own choice


def test_panels_and_raw_links_carry_the_page_farm(
    settings, session_factory, farm, other_farm
) -> None:
    # Parcel ids are farm-scoped: a panel loaded without the page's `?farm=` would read the
    # cookie or default farm and 404.
    body = _drawer(settings, session_factory, f"/parcelapp/parcels/42?farm={other_farm.id}")
    assert f'hx-get="/parcelapp/debug/parcels/42?farm={other_farm.id}"' in body
    assert f'hx-get="/globalapp/debug/summary?farm={other_farm.id}"' in body
    assert f'href="/fields/parcels.geojson?farm={other_farm.id}"' in body
    plain = _drawer(settings, session_factory, "/parcelapp/parcels/42")
    assert 'hx-get="/parcelapp/debug/parcels/42"' in plain


def test_toggle_links_keep_the_page_query(settings, session_factory, farm) -> None:
    with _client(settings, session_factory) as c:
        c.cookies.set("admin_token", TOKEN)
        off = c.get("/debug/drawer", params={"path": "/fields/?farm=5&x=1"}).text
        on = c.get("/debug/drawer", params={"path": "/fields/?farm=5&x=1", "debug": "1"}).text
    assert 'href="/fields/?farm=5&amp;x=1&amp;debug=1"' in off
    assert 'href="/fields/?farm=5&amp;x=1&amp;debug=0"' in on
    assert '<dd class="break-all">/fields/</dd>' in on  # the path shown without the query


def test_drawer_shows_the_last_access_log_rows(settings, session_factory, session, farm) -> None:
    grant = ensure_owner_grant(session, farm)
    for i in range(60):
        session.add(
            AccessLog(
                actor=f"tg:{1000 + i}",
                farm_id=farm.id,
                grant_id=grant.id,
                resource="parcel",
                scope="fields:read",
                purpose="web",
            )
        )
    session.commit()
    body = _drawer(settings, session_factory, "/fields/")
    table = body.split('id="debug-access-log"', 1)[1].split("</table>", 1)[0]
    assert table.count("<tr>") == 50
    assert "tg:1059" in table and "tg:1009" not in table
    assert 'href="/chat/"' in body


def test_an_uninstalled_apps_panels_leave_the_drawer_at_once(
    settings, session_factory, farm
) -> None:
    with session_factory() as s:
        s.merge(AppModule(name="globalapp", state="uninstalled", version="9.9.9"))
        s.commit()
    body = _drawer(settings, session_factory, "/fields/")
    assert "/globalapp/debug/summary" not in body
    assert 'data-app="globalapp"' in body  # still listed as a loaded manifest
