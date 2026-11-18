"""First-run location: offline time zone and country, owner-only save, and per-farm local time.

The offline lookup is pure; the HTTP tests use synthetic farms and points. Values come from
published geography (Paris is France, the Atlantic is sea, Natural Earth places the FR/CH
boundary vertex), never from the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.chat.assistant.context import tool_context
from farmcore.apps.onboarding import geocode
from farmcore.core import clock, logs
from farmcore.core.access import ensure_owner_grant
from farmcore.core.farm_location import locate
from farmcore.core.models.canonical import Advice, Asset, DataGrant, Farm, Parcel
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.share.conftest import make_parcel
from tests.core.test_member_sessions import _account, _login

T_SPRAY = datetime(2024, 4, 10, 23, 30, tzinfo=UTC)
CLOCK = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("latitude", "longitude", "time_zone", "country", "question"),
    [
        (44.9, -0.2, "Europe/Paris", "FR", None),  # Saint-Émilion
        (48.8566, 2.3522, "Europe/Paris", "FR", None),  # Paris
        (46.43, 6.5, None, None, "border"),  # a Natural Earth FR/CH boundary vertex
        (0.0, -30.0, None, None, "sea"),  # Atlantic
    ],
)
def test_locate_derives_facts_offline_and_asks_on_sea_or_border(
    latitude: float,
    longitude: float,
    time_zone: str | None,
    country: str | None,
    question: str | None,
) -> None:
    found = locate(latitude, longitude)

    assert found.question == question
    if question is None:
        assert (found.time_zone, found.country) == (time_zone, country)
    elif question == "border":
        assert found.country in {"FR", "CH"}  # the lookup proposes one side, never saves it
    else:
        assert found.country is None


def test_first_run_saves_the_owner_point_as_the_forecast_input(client, session, settings) -> None:
    farm = ensure_default_farm(session, settings)
    session.commit()

    first = client.get("/onboarding/", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/onboarding/location"

    saved = client.post(
        "/onboarding/location",
        data={"latitude": "44.9", "longitude": "-0.2"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/today/"

    session.expire_all()
    farm = session.get(Farm, farm.id)
    assert farm.props["forecast_location"] == {"latitude": 44.9, "longitude": -0.2}
    assert farm.props["time_zone"] == "Europe/Paris"
    assert farm.props["country"] == "FR"
    assert farm.props["country_name"] == "France"

    # Moving the point derives fresh facts: stale manual fields never override a new point.
    moved = client.post(
        "/onboarding/location",
        data={
            "latitude": "48.8566",
            "longitude": "2.3522",
            "time_zone": "America/New_York",
            "country": "US",
        },
        follow_redirects=False,
    )
    assert moved.status_code == 303
    session.expire_all()
    props = session.get(Farm, farm.id).props
    assert props["forecast_location"] == {"latitude": 48.8566, "longitude": 2.3522}
    assert (props["time_zone"], props["country"]) == ("Europe/Paris", "FR")

    done = client.get("/onboarding/", follow_redirects=False)
    assert done.headers["location"] == "/today/"


def test_a_sea_point_asks_the_owner_and_their_choice_is_stored(client, session, settings) -> None:
    farm = ensure_default_farm(session, settings)
    session.commit()

    asked = client.post(
        "/onboarding/location",
        data={"latitude": "0", "longitude": "-30"},
        follow_redirects=False,
    )
    assert asked.status_code == 422
    assert "at sea" in asked.text
    session.expire_all()
    assert "forecast_location" not in (session.get(Farm, farm.id).props or {})

    chosen = client.post(
        "/onboarding/location",
        data={
            "latitude": "0",
            "longitude": "-30",
            "time_zone": "UTC",
            "country": "PT",
            "mode": "manual",
        },
        follow_redirects=False,
    )
    assert chosen.status_code == 303
    session.expire_all()
    props = session.get(Farm, farm.id).props
    assert (props["time_zone"], props["country"]) == ("UTC", "PT")


def test_only_an_owner_can_save_the_location(settings, committed_session_factory) -> None:
    with committed_session_factory.begin() as session:
        farm = Farm(name="Owner-only location farm")
        session.add(farm)
        session.flush()
        ensure_owner_grant(session, farm)
        farm_id = farm.id
    _account(committed_session_factory, login="location-owner", farm_id=farm_id, role="owner")
    _account(committed_session_factory, login="location-advisor", farm_id=farm_id, role="advisor")

    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as advisor:
        assert _login(advisor, login="location-advisor").status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": advisor.cookies.get("farmcore_csrf"),
        }
        assert advisor.get(f"/onboarding/location?farm={farm_id}").status_code == 200
        refused = advisor.post(
            "/onboarding/location",
            params={"farm": farm_id},
            data={"latitude": "44.9", "longitude": "-0.2"},
            headers=headers,
        )
        assert refused.status_code == 403

    with committed_session_factory() as session:
        assert "forecast_location" not in (session.get(Farm, farm_id).props or {})


def test_geocoder_is_off_by_default_and_sends_a_user_agent_when_configured(
    client, settings, session, session_factory, monkeypatch
) -> None:
    farm = ensure_default_farm(session, settings)
    session.commit()
    assert "Search an address" not in client.get("/onboarding/location").text
    assert client.post("/onboarding/location/search", data={"q": "Paris"}).status_code == 404

    configured = settings.model_copy(update={"geocoder_base_url": "https://nominatim.example"})
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/api"):  # Photon
            return httpx.Response(
                200,
                request=request,
                json={
                    "features": [
                        {
                            "geometry": {"coordinates": [-0.2, 44.9]},
                            "properties": {
                                "name": "Village Hall",
                                "city": "Saint-Émilion",
                                "country": "France",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            request=request,
            json=[{"lat": "44.9", "lon": "-0.2", "display_name": "Village Hall, France"}],
        )

    original = httpx.Client
    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(geocode.httpx, "Client", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(geocode, "_last_request_at", 0.0)
    app = create_app(configured, session_factory=session_factory)
    with TestClient(
        app,
        headers={"X-Admin-Token": configured.admin_token.get_secret_value()},
    ) as browser:
        page = browser.post(
            f"/onboarding/location/search?farm={farm.id}", data={"q": "Village Hall"}
        )

    assert page.status_code == 200
    assert "Village Hall, France" in page.text
    request = requests[0]
    agent = request.headers["User-Agent"]
    assert agent.startswith("farmcore/")
    assert settings.public_base_url in agent
    assert request.url.params["format"] == "jsonv2"

    monkeypatch.setattr(geocode, "_last_request_at", 0.0)
    [photon] = geocode.search(
        "https://photon.example/api", "Village", public_base_url=settings.public_base_url
    )
    assert (photon.label, photon.latitude, photon.longitude) == (
        "Village Hall, Saint-Émilion, France",
        44.9,
        -0.2,
    )


def _two_zone_farms(session, settings) -> tuple[Farm, Farm]:
    paris = Farm(name="Paris zone farm", props={"time_zone": "Europe/Paris"})
    new_york = Farm(name="New York zone farm", props={"time_zone": "America/New_York"})
    session.add_all([paris, new_york])
    session.flush()
    for farm in (paris, new_york):
        ensure_owner_grant(session, farm)
        session.add(Asset(farm_id=farm.id, kind="station", name=f"Station {farm.id}"))
        parcel = make_parcel(session, farm, 1)
        logs.record_operation(
            session,
            farm_id=farm.id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=T_SPRAY,
            ts_end=None,
            quantities=[logs.QuantityInput("rate", Decimal("1"), "L/ha", label="Copper")],
            source=logs.SourceInput("voice", f"tg:{farm.id}:1", {}, 0.9, logs.RANK_VOICE),
            actor="owner",
            occurred_at=T_SPRAY,
        )
    clock.set_replay(session, CLOCK, settings)
    session.commit()
    return paris, new_york


def test_register_shows_each_farms_own_local_time(client, session, settings) -> None:
    paris, new_york = _two_zone_farms(session, settings)

    paris_page = client.get(f"/operations/register?farm={paris.id}").text
    new_york_page = client.get(f"/operations/register?farm={new_york.id}").text
    assert "01:30" in paris_page and "19:30" not in paris_page
    assert "19:30" in new_york_page and "01:30" not in new_york_page


def test_today_shows_each_farms_own_local_time(client, session, settings) -> None:
    paris, new_york = _two_zone_farms(session, settings)
    for farm in (paris, new_york):
        parcel = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id))
        session.add(
            Advice(
                farm_id=farm.id,
                parcel_id=parcel.id,
                evaluated_at=CLOCK,
                decision="protected",
                reasons=[],
                products=[],
                props={"assessment_status": "evaluable"},
            )
        )
    session.commit()

    paris_page = client.get(f"/advisor/today?farm={paris.id}").text
    new_york_page = client.get(f"/advisor/today?farm={new_york.id}").text

    assert "Fri 12 Apr · 08:00" in paris_page
    assert "Fri 12 Apr · 02:00" in new_york_page


def test_sharing_shows_each_farms_own_local_date(client, session, settings) -> None:
    paris, new_york = _two_zone_farms(session, settings)
    for farm in (paris, new_york):
        session.add(
            DataGrant(
                farm_id=farm.id,
                grantee=f"Co-op {farm.id}",
                scopes=["logs:read"],
                purpose="advisory",
                valid_to=T_SPRAY,
            )
        )
    session.commit()

    paris_page = client.get(f"/share/?farm={paris.id}").text
    new_york_page = client.get(f"/share/?farm={new_york.id}").text

    assert "11 Apr 2024" in paris_page and "10 Apr 2024" not in paris_page
    assert "10 Apr 2024" in new_york_page and "11 Apr 2024" not in new_york_page


def test_assistant_tools_use_the_farms_time_zone(session, settings) -> None:
    farm = ensure_default_farm(session, settings)
    farm.props = {**(farm.props or {}), "time_zone": "America/New_York"}
    session.commit()

    ctx = tool_context(session, settings, chat_id=7, now=CLOCK)

    assert ctx.tz.key == "America/New_York"
