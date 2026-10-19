"""Farm switcher: `selected_farm` behind `get_repo`, `GET /farms/switcher`, `GET /farms/select`.

Farms here are invented test rows.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import AccessLog, Asset, Farm
from farmcore.core.seed import ensure_default_farm

HOME_WIDGET_URLS = (
    "/advisor/today?view=attention",
    "/stations/today?compact=1",
    "/forecast/home-weather",
    "/fields/today?desktop=1",
    "/activity/today",
    "/forecast/today",
    "/advisor/today?view=calm",
    "/data-sources/today",
)
# Expert off (N4): the data-source counts are an Expert detail, so Home does not load them.
SIMPLE_WIDGET_URLS = tuple(u for u in HOME_WIDGET_URLS if u != "/data-sources/today")


def _home_widget_requests(body: str) -> set[str]:
    paths = {urlsplit(url).path for url in HOME_WIDGET_URLS}
    return {
        url
        for raw in re.findall(r'hx-get="([^"]+)"', body)
        if urlsplit(url := html.unescape(raw)).path in paths
    }


@pytest.fixture
def farms(session, settings) -> tuple[Farm, Farm]:
    chateau = ensure_default_farm(session, settings)
    other = Farm(name="Second test farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    return chateau, other


def _farm_ids_read(session, since_id: int) -> set[int]:
    return set(session.scalars(select(AccessLog.farm_id).where(AccessLog.id > since_id)))


def _last_access_id(session) -> int:
    return session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0


def test_switcher_lists_farms_with_an_owner_grant_and_marks_the_selected(client, farms, session):
    chateau, other = farms
    ungranted = Farm(name="Farm without a grant")
    session.add(ungranted)
    session.commit()

    resp = client.get("/farms/switcher")

    assert resp.status_code == 200
    body = resp.text
    assert chateau.name in body and other.name in body
    assert ungranted.name not in body
    assert f'value="{chateau.id}" selected' in body
    assert f'value="{other.id}" selected' not in body


def test_select_sets_the_cookie_and_redirects_to_next(client, farms):
    _, other = farms

    resp = client.get(
        "/farms/select", params={"farm": other.id, "next": "/operations/"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/operations/"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{FARM_COOKIE}={other.id};")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert f'value="{other.id}" selected' in client.get("/farms/switcher").text


@pytest.mark.parametrize(
    "next_", ["//evil.example", "https://evil.example/", "evil", "/\\evil.example", ""]
)
def test_select_never_redirects_off_site(client, farms, next_):
    _, other = farms

    resp = client.get(
        "/farms/select", params={"farm": other.id, "next": next_}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_select_unknown_or_ungranted_farm_is_404(client, farms, session):
    ungranted = Farm(name="Farm without a grant")
    session.add(ungranted)
    session.commit()

    assert client.get("/farms/select", params={"farm": 999_999}).status_code == 404
    assert client.get("/farms/select", params={"farm": ungranted.id}).status_code == 404
    assert client.get("/farms/select", params={"farm": "x"}).status_code in (404, 422)


def test_pages_follow_the_cookie(client, farms, session):
    chateau, other = farms
    client.cookies.set(FARM_COOKIE, str(other.id))
    since = _last_access_id(session)

    assert client.get("/operations/register.json").status_code == 200

    assert _farm_ids_read(session, since) == {other.id}


def test_farm_query_parameter_wins_over_the_cookie(client, farms, session):
    chateau, other = farms
    client.cookies.set(FARM_COOKIE, str(other.id))
    since = _last_access_id(session)

    assert client.get("/operations/register.json", params={"farm": chateau.id}).status_code == 200

    assert _farm_ids_read(session, since) == {chateau.id}


@pytest.mark.parametrize("stale", ["999999", "not-a-number", ""])
def test_a_stale_cookie_falls_back_to_the_chateau(client, farms, session, stale):
    chateau, _ = farms
    client.cookies.set(FARM_COOKIE, stale)
    since = _last_access_id(session)

    assert client.get("/operations/register.json").status_code == 200

    assert _farm_ids_read(session, since) == {chateau.id}


@pytest.mark.parametrize("selector", [str(2**63), str(-(2**63) - 1)])
@pytest.mark.parametrize("transport", ["query", "cookie"])
@pytest.mark.parametrize("path", ["/forecast/busy", "/operations/register"])
def test_overflowing_farm_selector_falls_back_without_poisoning_the_request(
    client, farms, session, path, transport, selector
):
    chateau, _ = farms
    params = {"day": "2024-04-09", "busy": "1"} if path == "/forecast/busy" else {}
    if transport == "query":
        params["farm"] = selector
    else:
        client.cookies.set(FARM_COOKIE, selector)

    since = _last_access_id(session)
    response = client.get(path, params=params, follow_redirects=False)

    if path == "/forecast/busy":
        assert response.status_code == 303
        assert client.cookies[f"farmcore_forecast_busy_{chateau.id}"] == "2024-04-09"
        assert response.headers["location"] == "/forecast/today"
    else:
        assert response.status_code == 200
        assert _farm_ids_read(session, since) == {chateau.id}


def test_a_revoked_owner_grant_falls_back_to_the_chateau(client, farms, session):
    chateau, other = farms
    ensure_owner_grant(session, other).revoked_at = datetime.now(UTC)
    session.commit()
    client.cookies.set(FARM_COOKIE, str(other.id))
    since = _last_access_id(session)

    assert client.get("/operations/register.json").status_code == 200

    assert _farm_ids_read(session, since) == {chateau.id}


def test_base_page_carries_the_self_loading_switcher_slot(client, farms):
    body = client.get("/operations/").text

    assert 'hx-get="/farms/switcher"' in body


def test_switcher_returns_to_the_current_page_with_its_query(client, farms):
    resp = client.get(
        "/farms/switcher",
        headers={"HX-Current-URL": "http://testserver/operations/register?year=2024&x=1"},
    )

    assert 'name="next" value="/operations/register?year=2024&amp;x=1"' in resp.text


def test_switcher_never_offers_an_off_site_next(client, farms):
    resp = client.get("/farms/switcher", headers={"HX-Current-URL": "http://evil.example//x"})

    assert 'name="next" value="/"' in resp.text


def test_select_marks_the_cookie_secure_over_https(client, farms):
    _, other = farms

    plain = client.get("/farms/select", params={"farm": other.id}, follow_redirects=False)
    tls = client.get(
        "https://testserver/farms/select", params={"farm": other.id}, follow_redirects=False
    )

    assert "Secure" not in plain.headers["set-cookie"]
    assert "Secure" in tls.headers["set-cookie"]


def test_select_replaces_stale_farm_query_but_preserves_other_filters(client, farms):
    chateau, other = farms
    resp = client.get(
        "/farms/select",
        params={
            "farm": other.id,
            "next": f"/operations/register?farm={chateau.id}&year=2024&x=1#eu",
        },
        follow_redirects=False,
    )
    from urllib.parse import parse_qs, urlsplit

    target = urlsplit(resp.headers["location"])
    assert target.path == "/operations/register" and target.fragment == "eu"
    assert parse_qs(target.query) == {"farm": [str(other.id)], "year": ["2024"], "x": ["1"]}


def test_query_farm_survives_today_widgets_and_shell_navigation(client, farms):
    chateau, other = farms
    client.cookies.set(FARM_COOKIE, str(chateau.id))
    body = client.get("/today/", params={"farm": other.id}).text

    def qualified(urls):
        return {f"{url}{'&' if '?' in url else '?'}farm={other.id}" for url in urls}

    # Both initial loads and Retry URLs retain the variants and explicit farm.
    assert _home_widget_requests(body) == qualified(SIMPLE_WIDGET_URLS)
    client.cookies.set("expert", "1")
    expert_body = client.get("/today/", params={"farm": other.id}).text
    assert _home_widget_requests(expert_body) == qualified(HOME_WIDGET_URLS)
    client.cookies.delete("expert")
    assert f'href="/fields/?farm={other.id}"' in body
    for path in ("/", "/operations/register"):
        assert f'href="{path}?farm={other.id}"' in body
    assert f'hx-get="/farms/switcher?farm={other.id}"' in body
    assert client.cookies.get(FARM_COOKIE) == str(chateau.id)


def test_query_farm_survives_register_export_links(client, farms):
    _, other = farms
    body = client.get("/operations/register", params={"farm": other.id}).text
    assert f'href="/operations/register.json?farm={other.id}"' in body
    assert f'href="/operations/register.csv?farm={other.id}"' in body


def test_query_farm_survives_activity_links(client, farms):
    _, other = farms
    body = client.get("/activity/today", params={"farm": other.id}).text
    assert f'href="/activity/?farm={other.id}"' in body
    assert f'hx-get="/activity/today?farm={other.id}"' in body


def test_root_redirect_keeps_the_explicit_farm(client, farms):
    _, other = farms
    response = client.get("/", params={"farm": other.id}, follow_redirects=False)
    assert response.headers["location"] == f"/today/?farm={other.id}"


def test_unowned_query_does_not_turn_into_access_and_child_links_use_resolved_farm(
    client, farms, session
):
    chateau, other = farms
    ungranted = Farm(name="Unowned URL target")
    session.add(ungranted)
    session.commit()
    client.cookies.set(FARM_COOKIE, str(other.id))
    since = _last_access_id(session)
    body = client.get("/operations/register", params={"farm": ungranted.id}).text
    assert f'href="/operations/register.json?farm={other.id}"' in body
    assert _farm_ids_read(session, since) == {other.id}
    assert client.cookies.get(FARM_COOKIE) == str(other.id)


def test_sharing_form_and_redirect_preserve_the_selected_query_farm(client, farms, session):
    import html
    import re
    from datetime import timedelta

    from farmcore.core.identity import DUMMY_HASH
    from farmcore.core.models.canonical import DataGrant
    from farmcore.core.models.identity import LocalIdentity

    recipient = LocalIdentity(login="qa-context-recipient", password_hash=DUMMY_HASH)
    session.add(recipient)
    session.commit()
    chateau, other = farms
    client.cookies.set(FARM_COOKIE, str(chateau.id))
    page = client.get("/share/", params={"farm": other.id}).text
    action = html.unescape(re.search(r'<form method="post" action="([^"]+)"', page)[1])
    response = client.post(
        action,
        data={
            "grantee": "QA explicit farm",
            "recipient_login": recipient.login,
            "purpose": "synthetic context regression",
            "preset": "adviser_logs",
            "valid_to": (datetime.now(UTC) + timedelta(days=30)).date().isoformat(),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    grant = session.scalar(select(DataGrant).where(DataGrant.grantee == "QA explicit farm"))
    assert grant.farm_id == other.id
    assert grant.recipient_identity_id == recipient.id
    assert response.headers["location"] == f"/share/?farm={other.id}"
    assert client.cookies.get(FARM_COOKIE) == str(chateau.id)
    denied = client.post(
        f"/share/grants/{grant.id}/revoke?farm={chateau.id}", follow_redirects=False
    )
    assert denied.status_code == 404
    session.refresh(grant)
    assert grant.revoked_at is None
    page = client.get(response.headers["location"]).text
    action = html.unescape(re.search(r'action="([^"]+/revoke[^\"]*)"', page)[1])
    revoked = client.post(action, follow_redirects=False)
    assert revoked.status_code == 303
    session.refresh(grant)
    assert grant.revoked_at is not None
    assert revoked.headers["location"] == f"/share/?farm={other.id}"
    assert f'href="/share/as/{grant.id}/?farm=' not in page


def test_kpi_get_form_carries_explicit_farm_as_a_successful_control(client, farms):
    _, other = farms
    page = client.get("/kpi/", params={"farm": other.id}).text
    assert f'type="hidden" name="farm" value="{other.id}"' in page


@pytest.mark.parametrize("selector", ["invalid", "0", "-1", ""])
def test_invalid_today_selector_cannot_qualify_child_urls(client, farms, selector):
    chateau, _ = farms
    client.cookies.set(FARM_COOKIE, str(chateau.id))
    page = client.get("/today/", params={"farm": selector}).text
    assert _home_widget_requests(page) == set(SIMPLE_WIDGET_URLS)
    client.cookies.set("expert", "1")
    expert_page = client.get("/today/", params={"farm": selector}).text
    assert _home_widget_requests(expert_page) == set(HOME_WIDGET_URLS)
    assert client.cookies.get(FARM_COOKIE) == str(chateau.id)


@pytest.mark.parametrize("suffix", ["", "?farm={first}#hourly-weather"])
def test_switch_from_station_detail_opens_the_new_farms_weather(client, session, farms, suffix):
    first, second = farms
    station = Asset(farm_id=first.id, kind="station", name="First farm station")
    other_station = Asset(farm_id=second.id, kind="station", name="Second farm station")
    session.add_all([station, other_station])
    session.commit()
    source = f"/stations/{station.id}" + suffix.format(first=first.id)
    assert client.get(f"/stations/{station.id}?farm={first.id}").status_code == 200
    response = client.get(
        "/farms/select",
        params={"farm": second.id, "next": source},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/stations/?farm={second.id}"
    destination = client.get(response.headers["location"])
    assert destination.status_code == 200
    assert "Second farm station" in destination.text
    assert "First farm station" not in destination.text
