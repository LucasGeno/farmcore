"""A live tick backfills the farm's season from ERA5 for its pin: the hours land under an ERA5
grid-point station, flagged as model data, CC BY with the Copernicus notice. The CDS key never
reaches a receipt, the farm's health or a log line."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from farmcore.apps.weather_era5 import ingest
from farmcore.apps.weather_era5.manifest import MANIFEST
from farmcore.apps.weather_era5.provider import Era5, PortalError
from farmcore.core import clock
from farmcore.core.jobs import run_now
from farmcore.core.models.canonical import Farm
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.weather.contract import Point, TimeRange
from farmcore.core.weather.selection import select as select_sources
from farmcore.settings import Settings
from tests.apps.weather_era5.conftest import KEY, PIN
from tests.task_drain import drain


def _tick(session, session_factory, settings, farm, deadline: float | None = None) -> dict:
    manifest = MANIFEST
    if deadline is not None:  # a short attempt, as a worker with a slow queue would see
        job = replace(MANIFEST.jobs[0], deadline=timedelta(seconds=deadline))
        manifest = replace(MANIFEST, jobs=(job,))
    clock.set_live(session, settings)
    session.commit()
    run_now(session_factory, [manifest], ingest.JOB_NAME, settings)
    drain(session_factory, settings, [manifest])
    session.expire_all()
    return session.get(Farm, farm.id).props[ingest.HEALTH]


def test_a_live_tick_backfills_two_days_as_flagged_model_data_under_a_grid_point(
    session, session_factory, credentials, farm, cds
) -> None:
    fake = cds()

    health = _tick(session, session_factory, credentials, farm)

    assert health["error"] is None and health["pending"] is None
    # One order for the two whole days, at the grid point nearest the pin (0.25 degrees), as CSV.
    assert len(fake.submitted) == 1
    assert fake.submitted[0]["location"] == {"latitude": 45.0, "longitude": -0.25}
    # 48 hours, each a worked example: 288.15 K is 15 °C; 15 °C air with a 10 °C dew point is
    # 72 % humid (psychrometric tables); wind 3 and 4 m/s is 5 m/s = 18 km/h; 0.0005 m is 0.5 mm.
    rows = session.execute(
        select(WeatherObservation.variable, WeatherObservation.value).distinct()
    ).all()
    assert {(v, value.normalize()) for v, value in rows} == {
        ("air_temperature", Decimal("15")),
        ("relative_humidity", Decimal("72")),
        ("wind_speed", Decimal("18")),
        ("lwe_thickness_of_precipitation_amount", Decimal("0.5")),
    }
    assert session.scalar(select(func.count(WeatherObservation.id))) == 48 * 4
    # Flagged as model data: its own station (a grid point) and receipt kind; CC BY, shareable.
    station = session.scalars(select(WeatherStation)).one()
    assert (station.provider, station.provider_station_id) == ("copernicus-era5", "45.00,-0.25")
    receipt = session.scalars(select(WeatherReceipt)).one()
    assert receipt.request["endpoint_kind"] == "model-reanalysis"
    assert (receipt.licence, receipt.redistribution, receipt.farm_id) == (
        "CC-BY-4.0",
        True,
        farm.id,
    )
    assert "Copernicus Climate Change Service" in receipt.attribution


@pytest.mark.parametrize("fail", ["submit", "job"], ids=["key-refused", "job-failed"])
def test_a_failed_tick_records_why_without_the_key(
    session, session_factory, credentials, farm, cds, caplog, fail
) -> None:
    cds(fail=fail)

    health = _tick(session, session_factory, credentials, farm)

    assert health["error"] and health["pending"] is None
    assert session.scalar(select(func.count(WeatherObservation.id))) == 0
    assert KEY not in json.dumps(health) and KEY not in caplog.text


def test_an_order_still_queued_at_the_deadline_is_resumed_not_ordered_again(
    session, session_factory, credentials, farm, cds
) -> None:
    fake = cds(queued=10**9)

    health = _tick(session, session_factory, credentials, farm, deadline=0.5)

    assert health["pending"] and health["error"] is None
    assert session.scalar(select(func.count(WeatherObservation.id))) == 0
    fake.queued = 0  # the CDS finishes it
    session.execute(text("DELETE FROM task_attempt"))  # the next day's tick
    session.commit()

    health = _tick(session, session_factory, credentials, farm)

    assert len(fake.submitted) == 1  # the same job was collected
    assert health["pending"] is None
    assert session.scalar(select(func.count(WeatherObservation.id))) == 48 * 4


def _provider(timeout: float = 5.0) -> Era5:
    return Era5(key=KEY, base_url="https://cds.climate.copernicus.eu/api", timeout=timeout)


def test_a_throttled_order_waits_its_retry_after_then_succeeds(cds) -> None:
    fake = cds(throttle=1)
    end = datetime(2026, 1, 3, 23, tzinfo=UTC)

    fetched = _provider().fetch_observations(
        ingest.grid_station(Point(PIN["latitude"], PIN["longitude"])),
        TimeRange(end - timedelta(hours=24), end),
    )

    assert len(fake.submitted) == 1 and len(fetched.values) == 24 * 4


def test_the_cds_key_is_refused_over_plain_http_without_echoing_it() -> None:
    # Short, plain and last: pydantic prints the ends of its input beside the refusal.
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, cdsapi_url="http://x", cdsapi_key="s3cr3t")
    assert "s3cr3t" not in str(refused.value)


def test_selection_now_offers_era5_as_the_backfill_for_a_pin(credentials) -> None:
    from farmcore.apps.weather_era5.provider import provider

    chosen = select_sources(
        [provider(credentials)], Point(PIN["latitude"], PIN["longitude"]), "FR", None
    )

    assert [b.provider for b in chosen.backfill] == ["copernicus-era5"]
    assert chosen.stations == ()


def test_an_order_never_ready_ends_out_of_time_for_the_contract_call(cds) -> None:
    cds(queued=10**9)
    end = datetime(2026, 1, 3, 23, tzinfo=UTC)

    with pytest.raises(PortalError, match="out of time"):
        _provider(timeout=0.2).fetch_observations(
            ingest.grid_station(Point(PIN["latitude"], PIN["longitude"])),
            TimeRange(end - timedelta(hours=24), end),
        )
