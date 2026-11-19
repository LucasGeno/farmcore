"""Weather licences decide what leaves the installation; every weather view names its sources.

Failure model (one owning test each):
- a grant or an agent token reads weather values whose licence forbids redistribution or states
  none (`unknown`), through the repository every grantee and token read goes through;
- the owner loses their own non-redistributable data to the gate (it only applies to grant and
  token views; the backup bundle is a whole-database dump and never reads through it);
- a grant or token reads around the gate: a caller-built statement on the weather values, or
  the legacy `observation` rows, which carry no licence at all;
- the grantee export drops blocked data silently, or its notice leaks the blocked values;
- a weather view shows no attribution, or not which endpoint served the data.

Known limits: the NGSI-LD export and the grantee register carry no weather (the owner register's
saved spray weather is the farmer's own copy); stored advice is derived, not redistributed data.
Synthetic values only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.apps.share import service
from farmcore.core.access import ScopeDenied, authenticate_token
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.models.weather import WeatherForecast, WeatherObservation
from farmcore.core.weather import store
from farmcore.core.weather.contract import (
    Fetched,
    ForecastValue,
    Licence,
    ObservationValue,
    Request,
    Station,
)
from tests.apps.share.synth import NOW

HOUR = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
# Synthetic, recognisable values: one per source.
OPEN, NON_COMMERCIAL, UNSTATED = Decimal("11.5"), Decimal("27.125"), Decimal("31.375")
LEGACY = 41.625
SOURCES = (
    ("open-station", Licence("etalab-2.0", "Synthetic open network", True), "public", OPEN),
    ("nc-station", Licence("CC-BY-NC-4.0", "Synthetic NC network", False), None, NON_COMMERCIAL),
    ("quiet-station", Licence(None, None, True), None, UNSTATED),
)


@pytest.fixture
def weather(session, demo):
    """One observation and one forecast value per source on the demo farm, and one legacy
    `observation` row (no receipt, no licence)."""
    for n, (provider, licence, kind, value) in enumerate(SOURCES):
        for forecast in (False, True):
            fetched = Fetched(
                request=Request(dataset="forecast" if forecast else "hourly", endpoint_kind=kind),
                fetched_at=NOW,
                payload=f"{provider}-{forecast}".encode(),
                values=[],
            )
            receipt = store.record_receipt(
                session, provider=provider, licence=licence, fetched=fetched, farm_id=demo.farm.id
            )
            if forecast:
                store.store_forecast(
                    session,
                    receipt,
                    [ForecastValue("m", None, NOW, "air_temperature", "degC", value)],
                )
            else:
                station = store.upsert_station(
                    session, provider, Station(f"S{n}", f"Station {n}", 44.9, -0.15)
                )
                store.store_observations(
                    session,
                    receipt,
                    station,
                    [ObservationValue("air_temperature", "degC", value, HOUR, HOUR)],
                )
    asset = Asset(farm_id=demo.farm.id, kind="station", name="Legacy station")
    session.add(asset)
    session.flush()
    session.add(Observation(asset_id=asset.id, ts=HOUR, variable="t_c", value=LEGACY))
    session.commit()


@pytest.fixture
def station_grant(session, owner_repo, grantee_identity):
    grant = service.create_grant(
        owner_repo,
        recipient_identity_id=grantee_identity.id,
        grantee="Weather service",
        preset="station",
        purpose="forecast",
        valid_to=datetime.now(UTC) + timedelta(days=30),
        tz="Europe/Paris",
    )
    session.commit()
    return grant


def _token_repo(session, owner_repo, demo, password_hash):
    owner = LocalIdentity(login="licence-owner", password_hash=password_hash)
    session.add(owner)
    session.flush()
    session.add(FarmMembership(identity_id=owner.id, farm_id=demo.farm.id, role="owner"))
    _grant, raw = service.create_token(
        owner_repo,
        creator_id=owner.id,
        name="Weather agent",
        scopes=["stations:read"],
        purpose="forecast",
        valid_to=datetime.now(UTC) + timedelta(days=30),
        tz="Europe/Paris",
    )
    session.commit()
    return authenticate_token(session, raw)


@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        ("owner", {OPEN, NON_COMMERCIAL, UNSTATED}),
        ("grant", {OPEN}),
        ("token", {OPEN}),
    ],
)
def test_grants_and_tokens_read_only_redistributable_weather(
    reader, expected, session, demo, owner_repo, station_grant, weather, share_password_hash
) -> None:
    repo = {
        "owner": lambda: owner_repo,
        "grant": lambda: service.grant_repo(session, station_grant),
        "token": lambda: _token_repo(session, owner_repo, demo, share_password_hash),
    }[reader]()

    observed = {o.value for o in repo.list(WeatherObservation, scope="stations:read")}
    forecast = {f.value for f in repo.list(WeatherForecast, scope="stations:read")}

    assert observed == expected
    assert forecast == expected


@pytest.mark.parametrize(
    "read",
    [
        lambda repo: repo.execute(
            select(WeatherObservation), scope="stations:read", resource="weather_observation"
        ),
        lambda repo: repo.execute_unaudited(
            select(WeatherForecast), scope="stations:read", resource="weather_forecast"
        ),
        lambda repo: repo.list(Observation, scope="stations:read"),
    ],
    ids=["statement", "unaudited-statement", "legacy-rows"],
)
def test_a_grant_cannot_read_weather_around_the_licence_gate(
    read, session, station_grant, weather
) -> None:
    with pytest.raises(ScopeDenied):
        read(service.grant_repo(session, station_grant))


def test_the_grantee_export_omits_blocked_weather_and_says_so(
    grantee_client, station_grant, weather
) -> None:
    response = grantee_client.get(f"/share/as/{station_grant.id}/observations.json")
    page = grantee_client.get(f"/share/as/{station_grant.id}/").text

    assert response.status_code == 200
    body = response.json()
    assert [o["value"] for o in body["observations"]] == [float(OPEN)]
    assert {(s["provider"], s["attribution"]) for s in body["sources"]} == {
        ("open-station", "Synthetic open network")
    }
    assert {(w["provider"], w["licence"]) for w in body["withheld"]} == {
        ("nc-station", "CC-BY-NC-4.0"),
        ("quiet-station", "unknown"),
    }
    for text in (response.text, page):
        assert not any(str(v) in text for v in (NON_COMMERCIAL, UNSTATED, LEGACY))
    assert "Synthetic open network" in page
    assert "nc-station" in page and "quiet-station" in page


def test_weather_page_shows_each_source_attribution_endpoint_and_sharing(admin, weather) -> None:
    page = admin.get("/stations/").text

    assert "Synthetic open network" in page
    assert "public endpoint" in page
    assert "Synthetic NC network" in page
    assert "Not shared with third parties" in page
