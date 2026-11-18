"""The replay station as a weather provider: what one `fetch_observations` response holds.

Failure modes: porting the CSV connector onto the contract drops, invents or shifts a value
(a null cell stored as 0, an hour outside the range, binary float noise, the wrong period); a
farm's own station asset is published as a Météo-France station.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from farmcore.apps.stations import connector
from farmcore.apps.stations.provider import MeteoFranceReplay, store_hours
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather.contract import BBox, TimeRange
from tests.apps.stations.synth import write_csv

T0 = datetime(2024, 4, 10, 4, tzinfo=UTC)
HOUR = timedelta(hours=1)


def test_a_fetch_holds_the_range_s_non_null_cells_in_cf_units_over_their_periods(tmp_path):
    write_csv(
        tmp_path,
        [
            (T0 - HOUR, "9", "9", "9", "9"),  # the range's start is excluded
            (T0, "0.1", "12.5", "", "7.2"),
            (T0 + HOUR, "", "13.0", "80", ""),
            (T0 + 2 * HOUR, "9", "9", "9", "9"),  # after the range
        ],
    )
    provider = MeteoFranceReplay(tmp_path)
    (station,) = provider.discover_stations(BBox(-1, 44, 0, 45))

    fetched = provider.fetch_observations(station, TimeRange(T0 - HOUR, T0 + HOUR))

    assert provider.discover_stations(BBox(2, 48, 3, 49)) == []
    assert sorted(
        (v.period_end, v.variable, v.unit, v.value, v.period_end - v.period_start)
        for v in fetched.values
    ) == [
        (T0, "air_temperature", "degC", Decimal("12.5"), timedelta(0)),
        (T0, "lwe_thickness_of_precipitation_amount", "mm", Decimal("0.1"), HOUR),
        (T0, "wind_speed", "km h-1", Decimal("7.2"), timedelta(minutes=10)),
        (T0 + HOUR, "air_temperature", "degC", Decimal("13"), timedelta(0)),
        (T0 + HOUR, "relative_humidity", "%", Decimal("80"), timedelta(0)),
    ]
    assert (provider.licence().identifier, provider.licence().redistribution) == (
        "etalab-2.0",
        True,
    )


def test_a_station_without_a_meteo_france_number_is_refused_and_nothing_is_stored(
    session, settings
):
    farm = ensure_default_farm(session, settings)
    own = Asset(farm_id=farm.id, kind="station", name="Own sensor", geom="SRID=4326;POINT(0 45)")
    session.add(own)
    session.flush()
    hour = connector.Hour(T0, {"rr1_mm": 0.2, "t_c": 12.0, "rh_pct": 80.0, "ff_kmh": 5.0})

    with pytest.raises(ValueError):
        store_hours(session, own, [hour], T0)

    for model in (Observation, WeatherObservation, WeatherReceipt, WeatherStation):
        assert session.scalar(select(func.count()).select_from(model)) == 0
