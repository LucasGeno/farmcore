"""Weather licences decide what leaves the installation; every weather view names its sources.

Failure model (one owning test each):
- a grant or an agent token reads weather values whose licence forbids redistribution or states
  none (`unknown`), through the repository every grantee and token read goes through;
- the owner loses their own non-redistributable data to the gate (it only applies to grant and
  token views; the backup bundle is a whole-database dump and never reads through it);
- a grant or token reads around the gate: a caller-built statement on the weather values (even
  under another resource label), the receipts of blocked sources, or the legacy `observation`
  rows, which carry no licence at all;
- the grantee export drops blocked data silently, or its notice leaks the blocked values;
- a grant view tells a farm with withheld weather from a farm with none (owner ruling
  2026-11-22: existence is information; the notice is generic, from the grant's scope);
- a weather view shows no attribution, or not which endpoint served the data.

Latent leaks, owned by the follow-up 'Gate weather in derived exports' (it blocks the first
non-redistributable observation provider): stored advice shared under `advice:read` carries raw
hourly inputs in its reasons, and the register CSV linked from the Sharing page carries saved
spray weather whose licence was not kept. Today's only observation provider (Météo-France,
etalab-2.0) is redistributable, so neither can carry blocked data yet. The NGSI-LD export and the
grantee register carry no weather. Synthetic values only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Integer, Text, column, exists, func, literal_column, select, union, values

from farmcore.apps.share import service
from farmcore.core.access import ScopeDenied, authenticate_token
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.models.weather import (
    WeatherForecast,
    WeatherObservation,
    WeatherReceipt,
    WeatherStation,
)
from farmcore.core.weather import attribution, store
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


def _record(session, farm_id, sources):
    """One observation and one forecast value per source on the farm."""
    for n, (provider, licence, kind, value) in enumerate(sources):
        for forecast in (False, True):
            fetched = Fetched(
                request=Request(dataset="forecast" if forecast else "hourly", endpoint_kind=kind),
                fetched_at=NOW,
                payload=f"{provider}-{forecast}".encode(),
                values=[],
            )
            receipt = store.record_receipt(
                session, provider=provider, licence=licence, fetched=fetched, farm_id=farm_id
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


@pytest.fixture
def weather(session, demo):
    """`_record` for every source on the demo farm, and one legacy `observation` row (no
    receipt, no licence)."""
    _record(session, demo.farm.id, SOURCES)
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
    receipts = {r.provider for r in repo.list(WeatherReceipt, scope="stations:read")}

    assert observed == expected
    assert forecast == expected
    assert receipts == {provider for provider, _, _, value in SOURCES if value in expected}


@pytest.mark.parametrize(
    "read",
    [
        lambda repo: repo.execute(
            select(WeatherObservation), scope="stations:read", resource="weather_observation"
        ),
        lambda repo: repo.execute_unaudited(
            select(WeatherForecast), scope="stations:read", resource="weather_forecast"
        ),
        lambda repo: repo.execute(
            select(WeatherObservation.value).join(WeatherStation),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.execute(
            select(WeatherStation.id, select(WeatherObservation.value).limit(1).scalar_subquery()),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.execute(
            union(select(WeatherStation.name), select(func.cast(WeatherForecast.value, Text))),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.execute(
            select(WeatherStation.id).where(exists().where(WeatherObservation.value > 30)),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.execute(
            select(
                values(column("v", Integer))
                .data([(select(WeatherObservation.value).limit(1).scalar_subquery(),)])
                .alias("v")
                .c.v
            ),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.execute(
            select(WeatherStation.id, literal_column("(SELECT value FROM weather_observation)")),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.list(
            WeatherStation, exists().where(WeatherObservation.value > 30), scope="stations:read"
        ),
        lambda repo: repo.execute(
            select(WeatherStation.id).prefix_with("(SELECT count(*) FROM weather_observation),"),
            scope="stations:read",
            resource="weather_station",
        ),
        lambda repo: repo.list(
            WeatherStation,
            select(WeatherObservation.id).where(WeatherObservation.value > 30).subquery().c.id > 0,
            scope="stations:read",
        ),
        lambda repo: repo.list(
            WeatherStation, WeatherReceipt.redistribution.is_(False), scope="stations:read"
        ),
        lambda repo: repo.list(
            WeatherStation, scope="stations:read", order_by=WeatherObservation.value
        ),
        lambda repo: repo.list(
            WeatherStation,
            scope="stations:read",
            limit=literal_column("(SELECT count(*) FROM weather_observation)"),
        ),
        lambda repo: store.held(
            repo, as_of=select(func.max(WeatherObservation.period_end)).scalar_subquery()
        ),
        lambda repo: repo.list(Observation, scope="stations:read"),
    ],
    ids=[
        "statement",
        "unaudited-statement",
        "mislabelled-statement",
        "scalar-subquery",
        "union",
        "exists-oracle",
        "values-payload",
        "embedded-text",
        "list-predicate",
        "statement-prefix",
        "list-cross-join",
        "list-receipt-cross-join",
        "list-ordering",
        "list-limit",
        "held-as-of",
        "legacy-rows",
    ],
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
    assert body["withheld"] == attribution.WITHHELD_NOTE
    for text in (response.text, page):
        assert not any(str(v) in text for v in (NON_COMMERCIAL, UNSTATED, LEGACY))
        assert "nc-station" not in text and "quiet-station" not in text
    assert "Synthetic open network" in page
    assert attribution.WITHHELD_NOTE in page


def test_a_grant_view_cannot_tell_withheld_weather_from_none(
    session, grantee_client, demo, station_grant
) -> None:
    def seen():
        export = grantee_client.get(f"/share/as/{station_grant.id}/observations.json")
        page = grantee_client.get(f"/share/as/{station_grant.id}/")
        assert export.status_code == page.status_code == 200
        return export.json(), page.text

    nothing = seen()
    _record(
        session,
        demo.farm.id,
        [s for s in SOURCES if not s[1].redistribution or s[1].identifier is None],
    )
    session.commit()

    assert seen() == nothing


@pytest.mark.parametrize(
    ("final", "shown"),
    [(SOURCES[0][1], Decimal("12.25")), (SOURCES[1][1], OPEN)],
    ids=["shared-final-replaces", "withheld-final-never-hides"],
)
def test_a_provisional_value_gives_way_on_a_grant_only_to_a_final_it_may_read(
    final, shown, session, grantee_client, demo, station_grant
) -> None:
    """`store.held` on a grant view (the export): a withheld final value for the same station,
    variable and period would otherwise hide the shared provisional one, a yes/no on withheld
    weather."""
    station = store.upsert_station(session, "open-station", Station("S0", "Station 0", 44.9, -0.15))
    for licence, value, provisional in (
        (SOURCES[0][1], OPEN, True),
        (final, Decimal("12.25"), False),
    ):
        fetched = Fetched(
            request=Request(dataset="hourly"),
            fetched_at=NOW - timedelta(minutes=5),
            payload=f"{licence.identifier}-{provisional}".encode(),
            values=[],
        )
        receipt = store.record_receipt(
            session,
            provider=f"{licence.identifier}",
            licence=licence,
            fetched=fetched,
            farm_id=demo.farm.id,
        )
        store.store_observations(
            session,
            receipt,
            station,
            [
                ObservationValue(
                    "air_temperature", "degC", value, HOUR, HOUR, provisional=provisional
                )
            ],
        )
    session.commit()

    export = grantee_client.get(f"/share/as/{station_grant.id}/observations.json")

    assert export.status_code == 200
    assert [o["value"] for o in export.json()["observations"]] == [float(shown)]


def test_a_grant_replays_weather_as_known_then_like_the_owner(
    session, demo, owner_repo, station_grant
) -> None:
    """`store.held(as_of=)`: a final value fetched after the replay time does not hide the
    provisional value known then, on a grant view as for the owner."""
    station = store.upsert_station(session, "open-station", Station("S0", "Station 0", 44.9, -0.15))
    final = Decimal("12.25")
    for value, provisional, fetched_at in (
        (OPEN, True, NOW - timedelta(days=2)),
        (final, False, NOW),
    ):
        fetched = Fetched(
            request=Request(dataset="hourly"),
            fetched_at=fetched_at,
            payload=f"replay-{provisional}".encode(),
            values=[],
        )
        receipt = store.record_receipt(
            session,
            provider="open-station",
            licence=SOURCES[0][1],
            fetched=fetched,
            farm_id=demo.farm.id,
        )
        store.store_observations(
            session,
            receipt,
            station,
            [
                ObservationValue(
                    "air_temperature", "degC", value, HOUR, HOUR, provisional=provisional
                )
            ],
        )
    session.commit()

    for repo in (owner_repo, service.grant_repo(session, station_grant)):
        assert [o.value for o in store.held(repo, as_of=NOW - timedelta(days=1))] == [OPEN]
        assert [o.value for o in store.held(repo, as_of=NOW)] == [final]


def test_weather_page_shows_each_source_attribution_endpoint_and_sharing(admin, weather) -> None:
    page = admin.get("/stations/").text

    assert "Synthetic open network" in page
    assert "public endpoint" in page
    assert "Synthetic NC network" in page
    assert "Not shared with third parties" in page
