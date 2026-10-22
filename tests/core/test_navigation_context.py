"""Contextual reading links through public HTTP routes, using invented farms only."""

import html
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import DataGrant, Event, Farm, Log
from farmcore.core.models.kernel import AppModule, ClockState
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.operations.conftest import make_parcel


@pytest.fixture
def farms(session, settings):
    first = ensure_default_farm(session, settings)
    second = Farm(name="Context second farm")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    now = get_clock(session, settings).now()
    complete_station(session, first.id, now)
    complete_station(session, second.id, now)
    mine = make_parcel(session, first, 101, area="1.25", props={})
    other = make_parcel(session, second, 202, area="2", props={})
    session.commit()
    return first, second, mine, other


def links(markup):
    return [html.unescape(url) for url in re.findall(r'href="([^"]+)"', markup)]


@pytest.mark.parametrize("app", ["advisor", "operations"])
def test_map_fragment_detail_returns_to_selected_parcel_and_resolved_farm(client, farms, app):
    first, second, mine, _ = farms
    # Cookie-only entry must produce an explicit link before another tab switches the cookie.
    client.cookies.set(FARM_COOKIE, str(first.id))
    panel = client.get(f"/{app}/map/parcels/{mine.id}")
    assert panel.status_code == 200
    [detail] = [u for u in links(panel.text) if urlsplit(u).path == f"/{app}/parcels/{mine.id}"]
    query = parse_qs(urlsplit(detail).query)
    assert query["farm"] == [str(first.id)]
    [origin] = query["return_to"]
    assert urlsplit(origin).path == "/fields/"
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(first.id)], "parcel": [str(mine.id)]}
    client.cookies.set(FARM_COOKIE, str(second.id))
    page = client.get(detail)
    assert page.status_code == 200
    assert "Back to Map" in page.text
    assert origin in links(page.text)
    returned = client.get(origin)
    assert returned.status_code == 200
    assert f'data-parcel-id="{mine.id}"' in returned.text
    assert "Test parcel 202" not in page.text


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example/fields/",
        "//evil.example/fields/",
        "/\\evil.example",
        "/fields/\n",
        "/fields/\t",
        "/fields/%00",
        "/fields/%2f",
        "/fields/%252f",
        "/fields/?parcel=%GG",
        "/fields/?farm=1&farm=1",
        "/fields/?return_to=/",
        "/fields/?token=secret",
        "/fields/?debug=1",
        "/fields/?parcel=999999999999999",
        "/fields/?parcel=-1",
        "/fields/?parcel=²",
        "/fields/?parcel=1&parcel=2",
        "/fields/#" + "x" * 2050,
        "/fields/#%0a",
        "/fields/#%ZZ",
        "/fields/panel",
        "/fields/list",
        "/advisor/parcels/1.json",
        "/clock/step",
        "/demo/reset",
        "/farms/select?farm=2",
        "/prefs/expert?enabled=1",
        "/forecast/busy?day=2024-04-09&busy=1",
        "/activity/columns?show=secret",
        "/operations/register.csv",
        "/notifications/read-all",
    ],
)
def test_hostile_origins_are_not_offered_or_followed(client, farms, session, origin):
    first, _, mine, _ = farms
    client.get(f"/advisor/parcels/{mine.id}", params={"farm": first.id})
    tables = (Farm, DataGrant, Log, Event, AppModule, ClockState)
    before = {m: session.execute(select(m.__table__)).all() for m in tables}
    cookies = dict(client.cookies)
    response = client.get(
        f"/advisor/parcels/{mine.id}", params={"farm": first.id, "return_to": origin}
    )
    assert response.status_code == 200
    assert "Back to Map" not in response.text
    assert "All advice" in response.text
    assert f"/advisor/?farm={first.id}" in links(response.text)
    assert dict(client.cookies) == cookies
    assert {m: session.execute(select(m.__table__)).all() for m in tables} == before


@pytest.mark.parametrize(
    ("origin", "label"),
    [
        ("/", "Back to Home"),
        ("/today/", "Back to Home"),
        ("/advisor/?model=powdery", "Back to Crop protection"),
        ("/forecast/plan#plan-day-2024-04-02", "Back to Plan"),
        (
            "/activity/?type=spray&from=2024-03-01&to=2024-04-01&before=2024-04-01T06%3A00%3A00%2B00%3A00&before_id=42",
            "Back to Activity",
        ),
        ("/operations/parcels/{parcel}#parcel-history", "Back to spray history"),
        ("/fields/parcels/{parcel}?view=overview", "Back to parcel"),
    ],
)
def test_recognized_reading_destinations_retain_only_their_valid_state(
    client, farms, origin, label
):
    first, _, mine, _ = farms
    origin = origin.format(parcel=mine.id)
    response = client.get(
        f"/advisor/parcels/{mine.id}", params={"farm": first.id, "return_to": origin}
    )
    assert response.status_code == 200
    assert label in response.text
    returned = [u for u in links(response.text) if urlsplit(u).path == urlsplit(origin).path]
    expected = parse_qs(urlsplit(origin).query) | {"farm": [str(first.id)]}
    assert any(
        parse_qs(urlsplit(target).query) == expected
        and urlsplit(target).fragment == urlsplit(origin).fragment
        for target in returned
    )


@pytest.mark.parametrize("app", ["advisor", "operations"])
def test_farm_switch_from_contextual_legacy_detail_drops_old_parcel_and_origin(client, farms, app):
    first, second, mine, _ = farms
    source = f"/{app}/parcels/{mine.id}?farm={first.id}&return_to=%2Ffields%2F"
    response = client.get(
        "/farms/select", params={"farm": second.id, "next": source}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/fields/?farm={second.id}"
    assert client.cookies[FARM_COOKIE] == str(second.id)


def test_unavailable_advice_still_has_the_contextual_return(client, farms, session):
    first, _, mine, _ = farms
    first.props = {"advisor_unavailable_reason": "Synthetic advice withheld"}
    session.commit()
    origin = f"/fields/?parcel={mine.id}&farm={first.id}"
    response = client.get(
        f"/advisor/parcels/{mine.id}", params={"farm": first.id, "return_to": origin}
    )
    assert response.status_code == 200
    assert "Synthetic advice withheld" in response.text
    assert "Back to Map" in response.text
    assert origin in links(response.text)


def test_map_list_links_name_the_clicked_parcel_as_origin_without_rewriting_json(client, farms):
    first, _, mine, _ = farms
    response = client.get("/fields/", params={"farm": first.id})
    [detail] = [u for u in links(response.text) if urlsplit(u).path == f"/fields/parcels/{mine.id}"]
    query = parse_qs(urlsplit(detail).query)
    [origin] = query["return_to"]
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(first.id)], "parcel": [str(mine.id)]}
    features = client.get("/fields/parcels.geojson", params={"farm": first.id}).json()["features"]
    assert features[0]["properties"]["card_url"] == f"/advisor/parcels/{mine.id}?farm={first.id}"


def test_history_timeline_remains_scoped_after_cookie_changes(client, farms):
    first, second, mine, _ = farms
    client.cookies.set(FARM_COOKIE, str(second.id))
    page = client.get(
        f"/operations/parcels/{mine.id}", params={"farm": first.id, "return_to": "/fields/"}
    )
    [timeline] = [
        html.unescape(u) for u in re.findall(r'hx-get="([^"]+)"', page.text) if "/timeline" in u
    ]
    assert timeline == f"/operations/parcels/{mine.id}/timeline?farm={first.id}"
    assert client.get(timeline).status_code == 200


def test_return_to_a_switched_off_reading_app_recovers_to_map(client, farms, session):
    first, _, mine, _ = farms
    session.get(AppModule, "activity").state = "uninstalled"
    session.commit()
    page = client.get(
        f"/advisor/parcels/{mine.id}",
        params={"farm": first.id, "return_to": "/activity/?type=spray"},
    )
    assert page.status_code == 200
    assert "Back to Map" in page.text
    assert f"/fields/?farm={first.id}" in links(page.text)


@pytest.mark.parametrize("app", ["advisor", "operations"])
@pytest.mark.parametrize("tour", [False, True])
def test_foreign_and_conflicting_origins_do_not_change_scope_or_business_state(
    client, farms, session, app, tour
):
    first, second, mine, other = farms
    client.get(f"/{app}/parcels/{mine.id}", params={"farm": first.id})  # initialize the clock
    if tour:
        client.cookies.set("farmcore_tour", "1")
    tables = (Farm, DataGrant, Log, Event, AppModule, ClockState)
    before = {m: session.execute(select(m.__table__)).all() for m in tables}
    origins = [
        f"/fields/?farm={second.id}&parcel={other.id}",
        f"/fields/?farm={first.id}&parcel={other.id}",
        f"/operations/parcels/{other.id}?farm={first.id}",
        f"/fields/parcels/{other.id}?farm={first.id}",
        f"/activity/?parcel_id={other.id}&farm={first.id}",
        "/activity/?type=erase",
        "/activity/?from=tomorrow",
        "/activity/?before=invalid",
        "/activity/?before_id=42",
        "/activity/?from=2024-04-02&to=2024-04-01",
        "/advisor/?model=wrong",
        "/forecast/plan?model=downy",
        "/?parcel_id=1",
    ]
    for origin in origins:
        response = client.get(
            f"/{app}/parcels/{mine.id}", params={"farm": first.id, "return_to": origin}
        )
        assert response.status_code == 200
        assert "Back to " not in response.text
        assert "Test parcel 202" not in response.text
        if tour:
            assert "/operations/chat" not in links(response.text)
        response = client.get(
            f"/{app}/parcels/{mine.id}",
            params=[("farm", str(first.id)), ("return_to", "/fields/"), ("return_to", "/")],
        )
        assert response.status_code == 200 and "Back to " not in response.text
    assert {m: session.execute(select(m.__table__)).all() for m in tables} == before


@pytest.mark.parametrize(
    ("app", "label"), [("advisor", "All advice"), ("operations", "Operations")]
)
def test_direct_legacy_entry_retains_its_same_farm_fallback(client, farms, app, label):
    first, _, mine, _ = farms
    client.cookies.set(FARM_COOKIE, str(first.id))
    page = client.get(f"/{app}/parcels/{mine.id}")
    assert page.status_code == 200
    assert label in page.text and "Back to " not in page.text
    assert f"/{app}/?farm={first.id}" in links(page.text)
    if app == "advisor":
        canonical = client.get(f"/advisor/parcels/{mine.id}.json").json()
        assert "return_to" not in str(canonical)


def test_cookie_only_map_freezes_data_panel_retry_and_poll_urls_before_cookie_changes(
    client, farms
):
    first, second, mine, _ = farms
    client.cookies.set(FARM_COOKIE, str(first.id))
    page = client.get("/fields/", params={"parcel": mine.id}).text
    metadata = json.loads(html.unescape(re.search(r"data-layers='([^']*)'", page)[1]))
    urls = [layer["url"] for layer in metadata]
    urls += [
        html.unescape(u)
        for u in re.findall(
            r'(?:hx-get|data-geojson-url|data-panel-farm-url|data-panel-parcel-url)="([^"]+)"', page
        )
        if urlsplit(html.unescape(u)).path.startswith(
            ("/fields/", "/advisor/map/", "/operations/map/")
        )
    ]
    assert urls
    assert all(parse_qs(urlsplit(u).query).get("farm") == [str(first.id)] for u in urls)
    client.cookies.set(FARM_COOKIE, str(second.id))
    for url in urls:
        response = client.get(url.replace("{id}", str(mine.id)))
        assert response.status_code == 200, url
        assert "Test parcel 202" not in response.text
    for app in ("advisor", "operations"):
        fragment = client.get(f"/{app}/map/parcels/{mine.id}", params={"farm": first.id}).text
        [poll] = [html.unescape(u) for u in re.findall(r'hx-get="([^"]+)"', fragment)]
        assert parse_qs(urlsplit(poll).query)["farm"] == [str(first.id)]


@pytest.mark.parametrize(
    "origin",
    [
        "/fields/parcels/{parcel}?view=unknown",
        "/fields/parcels/{parcel}?view=overview&type=spray",
        "/fields/#unsupported:anchor",
        "/forecast/plan#today",
    ],
)
def test_unsupported_workspace_state_and_anchors_keep_the_legacy_fallback(client, farms, origin):
    first, _, mine, _ = farms
    page = client.get(
        f"/advisor/parcels/{mine.id}",
        params={"farm": first.id, "return_to": origin.format(parcel=mine.id)},
    )
    assert page.status_code == 200
    assert "All advice" in page.text and "Back to " not in page.text
    assert f"/advisor/?farm={first.id}" in links(page.text)


def test_plan_reading_origin_retains_selected_day_and_day_anchor(client, farms):
    first, _, mine, _ = farms
    origin = f"/forecast/plan?day=2024-04-03&farm={first.id}#plan-day-2024-04-03"
    body = client.get(
        f"/advisor/parcels/{mine.id}", params={"farm": first.id, "return_to": origin}
    ).text
    assert "Back to Plan" in body and origin in links(body)
    for day in ("not-a-day", "2024-02-31", "20240403", "2024-04-03T00:00:00Z"):
        invalid = f"/forecast/plan?day={day}"
        body = client.get(
            f"/advisor/parcels/{mine.id}", params={"farm": first.id, "return_to": invalid}
        ).text
        assert invalid not in links(body) and "All advice" in body
