"""Stations service (contract for advisor 13 and baseline 15). Reads go through the
`ScopedRepository` with scope `stations:read`, never a raw session."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from farmcore.apps.stations.connector import VARIABLES
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Asset, ExtId
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.weather.variables import VARIABLES as CF

SCOPE = "stations:read"


def station_for_farm(repo: ScopedRepository) -> Asset | None:
    """The farm's weather station (the first by id), or None."""
    rows = repo.list(Asset, Asset.kind == "station", scope=SCOPE, order_by=Asset.id, limit=1)
    return rows[0] if rows else None


def weather_stations_of(asset_id: int) -> Any:
    """The normalised weather station(s) of a station asset: provider and station ID are one of
    the asset's external keys."""
    return (
        select(WeatherStation.id)
        .join(
            ExtId,
            (ExtId.source == WeatherStation.provider)
            & (ExtId.key == WeatherStation.provider_station_id),
        )
        .where(ExtId.model == "asset", ExtId.res_id == asset_id)
    )


def latest_reading(
    repo: ScopedRepository, asset_id: int, until: datetime | None = None
) -> datetime | None:
    """The newest reading hour the farm holds for the asset (at or before `until` if given):
    the end of its latest normalised observation period."""
    stmt = select(func.max(WeatherObservation.period_end)).where(
        WeatherObservation.station_id.in_(weather_stations_of(asset_id)),
        WeatherObservation.receipt_id.in_(
            select(WeatherReceipt.id).where(WeatherReceipt.farm_id == repo.farm_id)
        ),
    )
    if until is not None:
        stmt = stmt.where(WeatherObservation.period_end <= until)
    return repo.execute(stmt, scope=SCOPE, resource="weather_observation").scalar()


def load_hourly(
    repo: ScopedRepository, asset_id: int, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Hourly rows with `start <= ts <= end`, oldest first:
    `{ts, rr1_mm, t_c, rh_pct, ff_kmh}`, a variable missing that hour is None (never 0).
    Only hours with at least one stored value appear. Callers bound `end` (replay: clock now
    for observed values, now + 72 h for the replay forecast).

    Reads the normalised observations of the asset's provider station (the weather station
    whose provider and station ID are one of the asset's external keys), keyed by the end of
    each value's period, through the farm's receipts."""
    obs = repo.list(
        WeatherObservation,
        WeatherObservation.station_id.in_(weather_stations_of(asset_id)),
        WeatherObservation.period_end >= start,
        WeatherObservation.period_end <= end,
        scope=SCOPE,
        order_by=(WeatherObservation.period_end, WeatherObservation.variable),
    )
    by_ts: dict[datetime, dict[str, Any]] = {}
    for o in obs:
        row = by_ts.setdefault(o.period_end, {"ts": o.period_end, **dict.fromkeys(VARIABLES)})
        legacy = CF[o.variable].legacy_name
        if legacy in VARIABLES:
            row[legacy] = float(o.value)
    return list(by_ts.values())


def reading_when(ts: datetime, now: datetime, zone: ZoneInfo) -> str:
    """A reading time in words: `today 14:00`, `tomorrow 07:00` or `Fri 07:00` (local)."""
    local, days = (
        ts.astimezone(zone),
        (ts.astimezone(zone).date() - now.astimezone(zone).date()).days,
    )
    day = {0: "today", 1: "tomorrow"}.get(days, local.strftime("%a"))
    return f"{day} {local:%H:%M}"
