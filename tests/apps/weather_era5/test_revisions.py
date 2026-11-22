"""ERA5T is provisional until the final ERA5 replaces it, and each farm has its own grid point.

Failure model (one test each):
- Revision: a final ERA5 value never replaces the ERA5T one (first receipt wins), replaces it
  by overwriting (an as-of replay of the earlier time then reads the final value), or replaces
  it without a note saying what changed.
- Date rule: a value is marked final while its month may still be revised, or provisional
  long after.
- Re-request: the plan re-orders provisional hours every day, or never orders them again once
  they are old enough to be final.
- Two farms in one ERA5 cell: the second farm's hours are silently dropped (the first farm's
  are "held") and its ingest re-orders the season every day; a conflicting write to a shared
  station is dropped instead of refused.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from farmcore.apps.weather_era5 import ingest
from farmcore.apps.weather_era5 import provider as era5
from farmcore.apps.weather_era5.manifest import MANIFEST
from farmcore.core import clock
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.jobs import run_now
from farmcore.core.models.canonical import Farm
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.weather import merge, store
from farmcore.core.weather.contract import Fetched, Point, Request, Station, TimeRange
from tests.apps.weather_era5.conftest import KEY, PIN
from tests.task_drain import drain

HOUR_ = datetime(2026, 8, 10, 12, tzinfo=UTC)
AIR = "air_temperature"


def _repo(session, farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "era5")


def _fetched(kelvin: str, fetched_at: datetime) -> Fetched:
    """One CDS answer for `HOUR_` holding only the air temperature, parsed at `fetched_at`."""
    payload = f"valid_time,latitude,longitude,t2m\n{HOUR_:%Y-%m-%d %H:%M:%S},45,-0.25,{kelvin}\n"
    window = TimeRange(HOUR_ - timedelta(hours=1), HOUR_)
    return Fetched(
        request=Request(dataset=era5.DATASET, params={"date": [f"{HOUR_:%Y-%m-%d}"]}),
        fetched_at=fetched_at,
        payload=payload.encode(),
        values=era5.era5_values(payload.encode(), window, fetched_at=fetched_at),
    )


@pytest.mark.parametrize(
    ("fetched_at", "provisional"),
    [
        (datetime(2026, 8, 20, tzinfo=UTC), True),  # ERA5T, days behind real time
        (datetime(2026, 11, 30, 23, tzinfo=UTC), True),  # August ended under 3 months ago
        (datetime(2026, 12, 1, tzinfo=UTC), False),  # 3 months after August ended: final
    ],
    ids=["era5t", "month-not-old-enough", "final"],
)
def test_era5_values_are_provisional_until_their_month_ended_three_months_before(
    fetched_at, provisional
) -> None:
    (value,) = _fetched("288.15", fetched_at).values
    assert value.provisional is provisional


def test_final_era5_replaces_era5t_with_a_note_while_an_earlier_as_of_still_reads_era5t(
    session, farm
) -> None:
    provider = era5.Era5(key=KEY, base_url="https://cds.climate.copernicus.eu/api")
    station = ingest.grid_station(Point(PIN["latitude"], PIN["longitude"]), farm.id)
    early, late = datetime(2026, 8, 20, tzinfo=UTC), datetime(2026, 12, 2, tzinfo=UTC)
    ingest.store(session, farm.id, provider, station, _fetched("288.15", early))  # 15 °C
    ingest.store(session, farm.id, provider, station, _fetched("287.65", late))  # 14.5 °C
    session.flush()
    repo = _repo(session, farm)

    # Both stay stored (append only): nothing was overwritten.
    stored = session.execute(
        select(WeatherObservation.provisional, WeatherObservation.value).order_by(
            WeatherObservation.id
        )
    ).all()
    assert [(p, v.normalize()) for p, v in stored] == [
        (True, Decimal("15")),
        (False, Decimal("14.5")),
    ]
    # The current read: the final value, with what it replaced.
    (now,) = store.held(repo, WeatherObservation.variable == AIR, as_of=late)
    assert (now.value.normalize(), now.provisional) == (Decimal("14.5"), False)
    assert now.revision_note == "final replaces provisional 15 → 14.5"
    # An as-of read before the final receipt: the ERA5T value, as it was known then.
    (then,) = store.held(repo, WeatherObservation.variable == AIR, as_of=late - timedelta(days=1))
    assert (then.value.normalize(), then.provisional) == (Decimal("15"), True)
    # The merge layer reads the same rule.
    for as_of, expected in ((late, 14.5), (late - timedelta(days=1), 15.0)):
        (hour,) = merge.farm_hourly(repo, HOUR_, HOUR_, None, as_of=as_of).hours
        assert hour.values["t_c"] == expected


def _tick(session, session_factory, settings) -> None:
    """One live run for every due farm; the next run is made due (a day has passed)."""
    clock.set_live(session, settings)
    session.commit()
    run_now(session_factory, [MANIFEST], ingest.JOB_NAME, settings)
    drain(session_factory, settings, [MANIFEST])
    session.execute(
        text("UPDATE task_attempt SET finished_at = finished_at - :later"),
        {"later": ingest.INTERVAL + timedelta(minutes=5)},
    )
    session.commit()
    session.expire_all()


def _own_hours(session, farm) -> int:
    return session.scalar(
        select(func.count(WeatherObservation.id))
        .join(WeatherReceipt, WeatherReceipt.id == WeatherObservation.receipt_id)
        .join(WeatherStation, WeatherStation.id == WeatherObservation.station_id)
        .where(WeatherReceipt.farm_id == farm.id, WeatherStation.owner_farm_id == farm.id)
    )


def test_two_farms_in_one_era5_cell_each_hold_their_own_hours_and_stop_reordering(
    session, session_factory, credentials, farm, cds
) -> None:
    fake = cds()
    neighbour = Farm(
        name="Neighbour", props={"forecast_location": {"latitude": 44.95, "longitude": -0.22}}
    )
    session.add(neighbour)
    session.flush()
    ensure_owner_grant(session, neighbour)
    session.commit()

    _tick(session, session_factory, credentials)

    # Same cell, one order each, each farm's 48 hours under its own private grid point.
    assert [s["location"] for s in fake.submitted] == [{"latitude": 45.0, "longitude": -0.25}] * 2
    assert (_own_hours(session, farm), _own_hours(session, neighbour)) == (48 * 4, 48 * 4)
    for f in (farm, neighbour):
        assert len(merge.farm_hourly(_repo(session, f), *_season(), None, as_of=_now()).hours) == 48

    _tick(session, session_factory, credentials)

    assert len(fake.submitted) == 2  # every held hour is still provisional: nothing re-ordered


def _now() -> datetime:
    return datetime.now(UTC)


def _season() -> tuple[datetime, datetime]:
    now = _now()
    return now - timedelta(days=12), now


def test_a_conflicting_write_to_a_shared_station_from_another_farm_is_refused(session) -> None:
    a, b = Farm(name="A"), Farm(name="B")
    session.add_all([a, b])
    session.flush()
    public = Station("PUB-1", "Shared public station", 45.0, -0.2)
    provider = era5.Era5(key=KEY, base_url="https://cds.climate.copernicus.eu/api")
    first = _fetched("288.15", datetime(2026, 8, 20, tzinfo=UTC))
    store.receive(session, provider, first, station=public, farm_id=a.id)

    with pytest.raises(store.WeatherValueError, match="another farm"):
        store.receive(
            session, provider, replace(first, payload=b"b's copy"), station=public, farm_id=b.id
        )


def test_provisional_hours_are_ordered_again_once_old_enough_to_be_final(
    session, session_factory, credentials, farm, cds, monkeypatch
) -> None:
    fake = cds()
    _tick(session, session_factory, credentials)
    assert len(fake.submitted) == 1  # 48 provisional hours, held while still provisional

    # Time passes: those hours' months are now old enough to be final.
    monkeypatch.setattr(era5, "FINAL_AFTER_MONTHS", -1)
    _tick(session, session_factory, credentials)

    assert len(fake.submitted) == 2  # ordered again, now final
    repo = _repo(session, farm)
    current = store.held(repo, WeatherObservation.variable == AIR, as_of=_now())
    assert len(current) == 48
    assert {(o.provisional, o.revision_note) for o in current} == {
        (False, "final confirms provisional 15")
    }

    _tick(session, session_factory, credentials)

    assert len(fake.submitted) == 2  # final hours are held for good
