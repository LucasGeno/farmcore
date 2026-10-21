"""Tour restrictions apply before routing, even to an authenticated administrator."""

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.core.models.canonical import Farm
from farmcore.main import create_app

WRITES = (
    "/clock/step",
    "/apps/activity/install",
    "/apps/activity/uninstall",
    "/operations/isoxml",
    "/share/grants",
    "/share/grants/1/revoke",
)


@pytest.mark.parametrize("path", WRITES)
@pytest.mark.parametrize("credential", ["header", "cookie", "unconfigured"])
def test_tour_blocks_writes_before_validation(settings, session_factory, path, credential):
    configured = settings.model_copy(
        update={"admin_token": None if credential == "unconfigured" else SecretStr("test-admin")}
    )
    app = create_app(configured, session_factory=session_factory)
    with TestClient(app) as client:
        client.cookies.set("farmcore_tour", "1")
        if credential == "cookie":
            client.cookies.set("admin_token", "test-admin")
        headers = {"X-Admin-Token": "test-admin"} if credential == "header" else {}
        response = client.post(path, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Tour mode is read-only."}


def test_landing_enters_tour_mode_before_rendering(settings, session_factory):
    configured = settings.model_copy(update={"admin_token": SecretStr("test-admin")})
    with TestClient(create_app(configured, session_factory=session_factory)) as client:
        client.cookies.set("admin_token", "test-admin")
        response = client.get("/tour")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "farmcore_tour=1" in cookie
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
        assert "Max-Age" not in cookie
        assert "Read-only tour" in response.text and "read-only" in response.text.lower()
        for path in ("/today/", "/fields/", "/operations/register", "/activity/", "/share/"):
            assert f'href="{path}"' in response.text
        assert "/share/as/" not in response.text
        assert 'id="nav-admin"' not in response.text
        assert 'hx-post="/clock/step"' not in client.get("/clock").text
        assert client.post("/clock/step").status_code == 403


def test_tour_get_does_not_repair_missing_owner_grant_or_clock(settings, session_factory):
    from sqlalchemy import delete, func, select

    from farmcore.core.clock import ClockState
    from farmcore.core.models.canonical import DataGrant
    from farmcore.core.seed import ensure_default_farm

    app = create_app(settings, session_factory=session_factory)
    with session_factory() as session:
        ensure_default_farm(session, settings)
        session.execute(delete(DataGrant))
        session.execute(delete(ClockState))
        session.commit()
    with TestClient(app) as client:
        client.cookies.set("farmcore_tour", "1")
        assert client.get("/share/").status_code == 403
        assert client.get("/clock").status_code == 403
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DataGrant)) == 0
        assert session.scalar(select(func.count()).select_from(ClockState)) == 0


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "TRACE", "CUSTOM"])
@pytest.mark.parametrize("value", ["1", "", "0"])
def test_tour_blocks_all_unsafe_methods_on_unknown_routes(settings, session_factory, method, value):
    with TestClient(
        create_app(settings, session_factory=session_factory),
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as client:
        client.cookies.set("farmcore_tour", value)
        assert client.request(method, "/future-webhook").status_code == 403
        # Safe requests retain the underlying application's response, including 404/405.
        assert client.get("/future-webhook").status_code == 404
        assert client.head("/future-webhook").status_code == 404
        assert client.options("/future-webhook").status_code == 404


def test_new_endpoint_is_blocked_before_its_handler(settings, session_factory):
    app = create_app(settings, session_factory=session_factory)
    reached = []

    @app.post("/future-webhook")
    def future():
        reached.append(True)
        return {"ok": True}

    with TestClient(app) as client:
        client.cookies.set("farmcore_tour", "1")
        assert client.post("/future-webhook", json={"valid": "payload"}).status_code == 403
        assert reached == []
        client.cookies.clear()
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert client.post("/future-webhook").status_code == 200
        assert reached == [True]


@pytest.mark.parametrize("credential", ["header", "cookie", "unconfigured"])
def test_tour_ui_preferences_and_read_only_routes(settings, session_factory, credential):
    from farmcore.core.clock import clock_row
    from farmcore.core.seed import ensure_default_farm

    configured = settings.model_copy(
        update={"admin_token": None if credential == "unconfigured" else SecretStr("test-admin")}
    )
    app = create_app(configured, session_factory=session_factory)
    with session_factory() as session:
        farm = ensure_default_farm(session, settings)
        farm_id = farm.id
        if credential == "unconfigured":
            configured.demo_mode = True
            configured.demo_farm_ids = [farm_id]
        clock_row(session, settings)
        session.commit()
    with TestClient(app, base_url="https://testserver") as client:
        if credential == "cookie":
            client.cookies.set("admin_token", "test-admin")
        if credential == "header":
            client.headers["X-Admin-Token"] = "test-admin"
        landing = client.get("/tour")
        assert "Secure" in landing.headers["set-cookie"]
        for path, forbidden in (
            ("/clock", 'hx-post="/clock/step"'),
            ("/apps", "/activity/uninstall"),
            ("/operations/", 'action="/operations/isoxml"'),
            ("/share/", 'action="/share/grants"'),
        ):
            response = client.get(path)
            expected = 403 if path == "/share/" and credential == "unconfigured" else 200
            assert response.status_code == expected, path
            assert forbidden not in response.text
        assert client.get("/chat/").status_code == 403
        assert client.get("/debug/drawer?debug=1").text == ""
        assert client.get("/advisor/debug/parcels/1").status_code == 403
        for path in (
            "/prefs/expert?on=1&next=/tour",
            f"/farms/select?farm={farm_id}&next=/tour",
            "/activity/columns?show=at&next=/tour",
        ):
            assert client.get(path, follow_redirects=False).status_code == 303
            assert client.cookies.get("farmcore_tour") == "1"
        assert client.post("/clock/step").status_code == 403


def test_tour_uses_selected_farm_parcel_and_optional_apps(settings, session_factory):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm
    from farmcore.core.seed import ensure_default_farm
    from tests.apps.share.conftest import make_parcel

    app = create_app(settings, session_factory=session_factory)
    with session_factory() as session:
        default = ensure_default_farm(session, settings)
        first = make_parcel(session, default, 1)
        other = Farm(name="Other farm")
        session.add(other)
        session.flush()
        ensure_owner_grant(session, other)
        second = make_parcel(session, other, 1)
        first_id, second_id, other_id = first.id, second.id, other.id
        session.commit()
    with TestClient(
        app, headers={"X-Admin-Token": settings.admin_token.get_secret_value()}
    ) as client:
        page = client.get(f"/tour?farm={other_id}")
        assert page.status_code == 200
        assert f'href="/advisor/parcels/{second_id}?farm={other_id}"' in page.text
        assert f'/advisor/parcels/{first_id}"' not in page.text
        assert f'href="/today/?farm={other_id}"' in page.text
    from farmcore.core.app_manifest import MANIFEST as CORE

    with TestClient(
        create_app(settings, manifests=[CORE], session_factory=session_factory),
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as client:
        page = client.get("/tour")
        assert page.status_code == 200
        assert 'href="/activity/"' not in page.text
        assert 'href="/advisor/' not in page.text


@pytest.mark.parametrize("credential", ["header", "cookie"])
def test_valid_write_requests_leave_database_unchanged(settings, session_factory, credential):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from farmcore.apps.operations.isoxml_demo import demo_taskdata
    from farmcore.core.clock import clock_row
    from farmcore.core.models.canonical import DataGrant
    from farmcore.core.seed import ensure_default_farm

    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr("test-admin")}),
        session_factory=session_factory,
    )
    with session_factory() as session:
        farm = ensure_default_farm(session, settings)
        clock_row(session, settings)
        grant = DataGrant(
            farm_id=farm.id, grantee="Synthetic adviser", scopes=["logs:read"], purpose="test"
        )
        session.add(grant)
        session.commit()
        grant_id = grant.id

    def snapshot():
        with session_factory() as session:
            names = session.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ).scalars()
            return {
                name: session.execute(
                    text(f'SELECT to_jsonb(t) FROM "{name}" t ORDER BY to_jsonb(t)::text')
                )
                .scalars()
                .all()
                for name in names
                if name != "spatial_ref_sys"
            }

    with TestClient(app) as client:
        if credential == "header":
            client.headers["X-Admin-Token"] = "test-admin"
        else:
            client.cookies.set("admin_token", "test-admin")
        client.cookies.set("farmcore_tour", "1")
        before = snapshot()
        requests = (
            ("/clock/step", {"data": {"to": "2024-04-10T06:00"}}),
            ("/apps/activity/install", {}),
            ("/apps/activity/uninstall", {}),
            (
                "/operations/isoxml",
                {"files": {"file": ("TASKDATA.XML", demo_taskdata(), "application/xml")}},
            ),
            (
                "/share/grants",
                {
                    "data": {
                        "grantee": "Synthetic coop",
                        "preset": "adviser_logs",
                        "purpose": "test",
                        "valid_to": (datetime.now(UTC) + timedelta(days=30)).date().isoformat(),
                    }
                },
            ),
            (f"/share/grants/{grant_id}/revoke", {}),
        )
        for path, payload in requests:
            assert client.post(path, **payload).status_code == 403
            assert snapshot() == before, path


@pytest.mark.parametrize("decision", ["spray", "wait"])
@pytest.mark.parametrize("path", ["/today/", "/advisor/", "/advisor/today"])
@pytest.mark.parametrize("credential", ["header", "cookie"])
def test_tour_hides_configured_telegram_logging_actions(
    settings, session_factory, decision, path, credential
):
    from datetime import UTC, datetime, timedelta

    from farmcore.core.clock import set_replay
    from farmcore.core.models.canonical import Advice
    from farmcore.core.seed import ensure_default_farm
    from tests.apps.share.conftest import make_parcel

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    configured = settings.model_copy(
        update={
            "admin_token": SecretStr("test-admin"),
            "telegram_bot_username": "@synthetic_tour_test_bot",
        }
    )
    app = create_app(configured, session_factory=session_factory)
    with session_factory() as session:
        farm = ensure_default_farm(session, configured)
        parcel = make_parcel(session, farm, 1)
        session.add(
            Advice(
                farm_id=farm.id,
                parcel_id=parcel.id,
                evaluated_at=now,
                decision=decision,
                reasons=[],
                products=[],
                next_window_start=now + timedelta(hours=1) if decision == "wait" else now,
                next_window_end=now + timedelta(hours=4),
                props={"headline": "", "flags": []},
            )
        )
        set_replay(session, now, configured)
        session.commit()
    with TestClient(app) as client:
        if credential == "header":
            client.headers["X-Admin-Token"] = "test-admin"
        else:
            client.cookies.set("admin_token", "test-admin")
        normal = client.get(path)
        assert normal.status_code == 200
        assert 'href="https://t.me/synthetic_tour_test_bot"' in normal.text
        assert "Log a spray" in normal.text
        if path.startswith("/advisor/"):
            assert f'data-decision="{decision}"' in normal.text
        assert client.get("/tour").status_code == 200
        toured = client.get(path)
        assert toured.status_code == 200
        assert "https://t.me/" not in toured.text
        assert "Log a spray" not in toured.text
        if path.startswith("/advisor/"):
            assert f'data-decision="{decision}"' in toured.text
        client.cookies.clear()
        if credential == "cookie":
            client.cookies.set("admin_token", "test-admin")
        assert 'href="https://t.me/synthetic_tour_test_bot"' in client.get(path).text


def _member_in_tour(settings, factory, role="owner"):
    """A signed-in farm member whose browser has opened the tour."""
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.clock import clock_row
    from tests.core.test_member_sessions import _account, _login

    _, farm_id = _account(factory, role=role)
    with factory() as session:  # an installation that has run once outside the tour
        ensure_owner_grant(session, session.get(Farm, farm_id))
        clock_row(session, settings)
        session.commit()
    browser = TestClient(
        create_app(settings, session_factory=factory), base_url="https://testserver"
    )
    assert _login(browser).status_code == 303
    assert browser.get("/tour").status_code == 200
    return browser, farm_id


def _post(browser, path, data=None, origin="https://testserver"):
    form = {"csrf_token": browser.cookies.get("farmcore_csrf"), **(data or {})}
    return browser.post(path, data=form, headers={"Origin": origin}, follow_redirects=False)


def test_owner_tour_shows_exit_banner_and_opens_sharing_read_only(
    settings, committed_session_factory
):
    browser, _ = _member_in_tour(settings, committed_session_factory)
    with browser:
        tour = browser.get("/tour")
        assert 'href="/share/"' in tour.text
        for path in ("/today/", "/share/", "/farms/members"):
            page = browser.get(path)
            assert page.status_code == 200, path
            assert "Exit tour" in page.text
            # Owner pages render read-only: the only forms that post leave the tour.
            posts = re.findall(r'<form[^>]*method="post"[^>]*action="([^"]+)"', page.text)
            assert sorted(posts) == ["/logout", "/tour/exit"], path


def test_advisor_in_tour_is_still_refused_owner_pages(settings, committed_session_factory):
    browser, _ = _member_in_tour(settings, committed_session_factory, role="advisor")
    with browser:
        for path in ("/share/", "/farms/members"):
            assert browser.get(path).status_code == 403, path


def test_exit_tour_returns_to_the_page_and_writes_work_again(settings, committed_session_factory):
    browser, farm_id = _member_in_tour(settings, committed_session_factory)
    with browser:
        production = {"mode": "organic"}
        assert (
            _post(browser, f"/operations/production?farm={farm_id}", production).status_code == 403
        )
        assert _post(browser, "/tour/exit", origin="https://evil.example").status_code == 403
        assert browser.cookies.get("farmcore_tour") == "1"
        left = _post(browser, "/tour/exit", {"next": "/share/?status=active"})
        assert left.status_code == 303
        assert left.headers["location"] == "/share/?status=active"
        assert browser.cookies.get("farmcore_tour") is None
        assert 'action="/tour/exit"' not in browser.get("/share/").text
        assert (
            _post(browser, f"/operations/production?farm={farm_id}", production).status_code == 303
        )
        for next_path in ("/tour", "//evil.example"):
            browser.get("/tour")
            assert _post(browser, "/tour/exit", {"next": next_path}).headers["location"] == "/"
    with committed_session_factory() as session:
        assert session.get(Farm, farm_id).props.get("production") == "organic"


def test_sign_out_in_tour_ends_the_session_and_the_tour(settings, committed_session_factory):
    browser, _ = _member_in_tour(settings, committed_session_factory)
    with browser:
        assert _post(browser, "/logout/").status_code == 403  # the allowlist is exact paths
        response = _post(browser, "/logout")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert browser.cookies.get("farmcore_session") is None
        assert browser.cookies.get("farmcore_tour") is None
        browser.cookies.set("farmcore_tour", "1")
        assert _post(browser, "/tour/exit", origin="https://evil.example").status_code == 403
