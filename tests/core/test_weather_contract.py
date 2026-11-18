"""Weather provider contract and the normalised weather schema (expand step, core_0008).

Failure modes owned here (one test each):
- Golden: adding the normalised schema, or mirroring legacy weather into it, changes what the
  stations, forecast, advisor or replay consumers produce. Frozen in
  `snapshots/weather_golden.json` before any schema change.
- Licence: a receipt without a licence is stored as redistributable (or is refused instead of
  stored as `unknown`), or a raw SQL writer can mark unknown-licence data redistributable.
- Credential: a provider credential named in `auth_schema()` is stored in a receipt's request.
- Units and values: an observation or forecast value is stored as a float, NaN, or in a unit
  other than its CF variable's unit; a legacy float gains binary noise when mirrored.
- Registry: a second provider under a taken name silently replaces the first, or an object
  missing a contract method registers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from farmcore.apps.forecast import store as forecast_store
from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.models import ForecastValue as LegacyForecastValue
from farmcore.apps.forecast.normalised import mirror_run
from farmcore.apps.stations import service as stations
from farmcore.apps.stations.normalised import mirror_station
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Advice, Asset, Event, Parcel
from farmcore.core.models.weather import WeatherForecast, WeatherObservation, WeatherReceipt
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather.contract import (
    AuthField,
    BBox,
    Capabilities,
    Fetched,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    TimeRange,
)
from farmcore.core.weather.registry import ProviderError, Registry
from farmcore.core.weather.store import CredentialInRequest, WeatherValueError, receive
from farmcore.core.weather.variables import VARIABLES
from tests.apps.advisor import scenes

GOLDEN = Path(__file__).parent / "snapshots" / "weather_golden.json"
manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
NOW = "2024-05-15 06:00"


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _without_ids(value):
    """Drop generated row ids (sequences are not rolled back between tests)."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k != "id" and not k.endswith("_id")}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


def _semantics(session, settings) -> dict:
    """What the weather consumers produce at NOW, without generated ids or wall-clock times."""
    farm = ensure_default_farm(session, settings)
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "golden")
    now = scenes.local(NOW)
    station = stations.station_for_farm(repo)
    hourly = stations.load_hourly(
        repo, station.id, now - timedelta(hours=24), now + timedelta(hours=72)
    )
    run = forecast_store.latest_run(repo, now)
    advice = session.execute(
        select(
            Parcel.alias,
            Advice.evaluated_at,
            Advice.decision,
            Advice.reasons,
            Advice.next_window_start,
            Advice.next_window_end,
            Advice.protected_until,
            Advice.products,
            Advice.props,
        )
        .join(Parcel, Parcel.id == Advice.parcel_id)
        .order_by(Advice.evaluated_at, Parcel.alias)
    ).all()
    events = session.execute(
        select(Event.kind, Event.occurred_at, Event.body, Event.payload).order_by(
            Event.occurred_at, Event.id
        )
    ).all()
    return _without_ids(
        json.loads(
            json.dumps(
                {
                    "station_hourly": [{k: _iso(v) for k, v in row.items()} for row in hourly],
                    "forecast": None
                    if run is None
                    else {
                        "source": run.source,
                        "issued_at": _iso(run.issued_at),
                        "available_at": _iso(run.available_at),
                        "label": run.label,
                        "as_issued": run.as_issued,
                        "series": [
                            [_iso(h.ts), h.rr1_mm, h.t_c, h.rh_pct, h.ff_kmh] for h in run.series
                        ],
                        "props": run.props,
                    },
                    "freshness": forecast_store.freshness(repo, now),
                    "advice": [[_iso(v) for v in row] for row in advice],
                    "events": [[_iso(v) for v in row] for row in events],
                },
                default=_iso,
                sort_keys=True,
            )
        )
    )


def _digests(semantics: dict) -> dict:
    """Readable decisions plus a SHA-256 per section of the canonical JSON (the full output is
    megabytes of advice evidence); a changed section names itself."""
    return {
        "decisions": [row[:3] for row in semantics["advice"]],
        **{
            name: sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()
            for name, part in semantics.items()
        },
    }


@pytest.mark.data
def test_golden_outputs_unchanged_with_legacy_weather_mirrored_into_new_schema(
    session, settings, seeded, step
):
    seeded()
    step("2024-05-14 06:00")
    step(NOW)
    # Mirror the station and the two latest forecast runs into the normalised schema.
    for station in session.scalars(select(Asset).where(Asset.kind == "station")):
        mirror_station(session, station, scenes.local(NOW))
    for run in session.scalars(select(ForecastRun).order_by(ForecastRun.issued_at.desc()).limit(2)):
        mirror_run(session, run)
    assert session.scalar(select(func.count()).select_from(WeatherReceipt)) == 3

    actual = _digests(_semantics(session, settings))

    assert actual == json.loads(GOLDEN.read_text())


T0 = datetime(2026, 6, 1, 12, tzinfo=UTC)
RAIN = VARIABLES["lwe_thickness_of_precipitation_amount"]


# A provider that states no licence yet claims its data may be redistributed.
UNSTATED = Licence(None, None, True)


class FakeProvider:
    """A provider over canned responses (no network)."""

    name = "fake-network"

    def __init__(self, licence: Licence = UNSTATED) -> None:
        self._licence = licence

    def discover_stations(self, bbox: BBox) -> list[Station]:
        return [STATION]

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        raise NotImplementedError

    def fetch_forecast(self, point: Point, models, run) -> Fetched:
        raise NotImplementedError

    def capabilities(self) -> Capabilities:
        return Capabilities(observations=True, forecast=False, variables=frozenset(VARIABLES))

    def licence(self) -> Licence:
        return self._licence

    def auth_schema(self) -> tuple[AuthField, ...]:
        return (AuthField("token", secret=True), AuthField("station_group", secret=False))


STATION = Station("ST1", "Test station", 41.6, -4.7, Decimal("700"))


def _fetched(*values, url="https://example.test/obs?station=ST1", params=None, payload=b"r1"):
    return Fetched(Request("hourly", url, params or {}), T0, payload, list(values))


def _rain(value, unit="mm", hour=0) -> ObservationValue:
    end = T0.replace(hour=hour)
    return ObservationValue(RAIN.name, unit, value, end - timedelta(hours=1), end)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_receipt_without_a_licence_is_unknown_and_never_redistributable(session):
    receipt = receive(session, FakeProvider(), _fetched(_rain(Decimal("0.2"))), station=STATION)
    assert (receipt.licence, receipt.redistribution) == ("unknown", False)

    # Raw SQL writers get the same: an omitted licence defaults to unknown, not redistributable,
    raw = session.execute(
        text(
            "INSERT INTO weather_receipt (provider, dataset, fetched_at) "
            "VALUES ('raw', 'd', now()) RETURNING licence, redistribution"
        )
    ).one()
    assert tuple(raw) == ("unknown", False)
    # and the database refuses unknown-licence data marked redistributable.
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text(
                "INSERT INTO weather_receipt (provider, dataset, fetched_at, redistribution) "
                "VALUES ('raw', 'd', now(), true)"
            )
        )


@pytest.mark.parametrize(
    "url, params",
    [
        ("https://example.test/obs?station=ST1&Token=s3cret", None),
        ("https://example.test/obs", {"token": "s3cret"}),
    ],
)
def test_a_request_naming_a_secret_credential_is_refused_and_nothing_stored(session, url, params):
    fetched = _fetched(_rain(Decimal("0.2")), url=url, params=params)

    with pytest.raises(CredentialInRequest):
        receive(session, FakeProvider(), fetched, station=STATION)
    assert _count(session, WeatherReceipt) == 0


@pytest.mark.parametrize(
    "value",
    [
        _rain(0.2),  # a float
        _rain(Decimal("NaN")),
        _rain(Decimal("0.2"), unit="kg m-2"),  # a valid CF unit, but not the stored one
        ObservationValue("rainfall", "mm", Decimal("0.2"), T0, T0),  # not a CF name we store
    ],
)
def test_a_value_outside_the_stored_shape_is_refused_before_anything_is_written(session, value):
    fetched = _fetched(_rain(Decimal("1.0"), hour=1), value)

    with pytest.raises(WeatherValueError):
        receive(session, FakeProvider(), fetched, station=STATION)
    assert _count(session, WeatherReceipt) == _count(session, WeatherObservation) == 0


def test_an_observation_already_held_keeps_its_value_and_receipt(session):
    provider = FakeProvider(Licence("CC-BY-4.0", "© Test", True))
    first = receive(session, provider, _fetched(_rain(Decimal("0.2"))), station=STATION)
    again = receive(session, provider, _fetched(_rain(Decimal("0.2"))), station=STATION)
    later = receive(
        session, provider, _fetched(_rain(Decimal("0.4")), payload=b"r2"), station=STATION
    )

    assert again.id == first.id  # the same response is the same receipt
    assert later.id != first.id
    rows = session.execute(select(WeatherObservation.value, WeatherObservation.receipt_id)).all()
    assert rows == [(Decimal("0.2"), first.id)]


def test_registry_refuses_a_taken_name_and_an_object_missing_a_method():
    registry = Registry()
    first = registry.register(FakeProvider())

    with pytest.raises(ProviderError):
        registry.register(FakeProvider())
    no_auth = type(
        "NoAuth", (), {k: v for k, v in vars(FakeProvider).items() if k != "auth_schema"}
    )
    with pytest.raises(ProviderError):
        Registry().register(no_auth())
    assert registry.get("fake-network") is first
    assert list(registry) == [first]


def test_frozen_weather_variable_rows_match_the_cf_variables(session):
    rows = session.execute(text("SELECT name, unit, legacy_name FROM weather_variable")).all()

    assert sorted(rows) == sorted((v.name, v.unit, v.legacy_name) for v in VARIABLES.values())


def test_legacy_forecast_runs_mirror_with_their_request_licence_and_exact_values(session, settings):
    farm = ensure_default_farm(session, settings)
    live = ForecastRun(
        farm_id=farm.id,
        source="open-meteo-live-receipt",
        issued_at=T0,
        available_at=T0,
        props={
            "issued_at_kind": "receipt",
            "fetched_at": T0.isoformat(),
            "request_url": "https://customer-api.open-meteo.com/v1/forecast?latitude=44.9",
            "endpoint_kind": "customer",
        },
    )
    saved = ForecastRun(farm_id=farm.id, source="synthetic", issued_at=T0, available_at=T0)
    session.add_all([live, saved])
    session.flush()
    session.add_all(
        LegacyForecastValue(farm_id=farm.id, run_id=run.id, ts=T0, variable=var, value=value)
        for run in (live, saved)
        for var, value in (("rr1_mm", 0.1), ("fx_kmh", 33.3))
    )
    session.flush()

    receipt = mirror_run(session, live)
    assert mirror_run(session, live).id == receipt.id  # mirroring again adds nothing
    unknown = mirror_run(session, saved)

    assert receipt.request["url"] == live.props["request_url"]
    assert receipt.request["endpoint_kind"] == "customer"
    assert (receipt.licence, receipt.redistribution) == ("CC-BY-4.0", True)
    assert (unknown.licence, unknown.redistribution) == ("unknown", False)
    rows = session.execute(
        select(
            WeatherForecast.variable,
            WeatherForecast.unit,
            WeatherForecast.value,
            WeatherForecast.run,
        )
        .where(WeatherForecast.receipt_id == receipt.id)
        .order_by(WeatherForecast.variable)
    ).all()
    # A live receipt's issued_at is its identity, not a model run time.
    assert rows == [
        ("lwe_thickness_of_precipitation_amount", "mm", Decimal("0.1"), None),
        ("wind_speed_of_gust", "km h-1", Decimal("33.3"), None),
    ]
