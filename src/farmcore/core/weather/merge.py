"""One hourly series per farm from every weather source it holds (spec W4).

Each station whose observations the farm holds through its own receipts is one source.
Sources rank by tier (`contract.Tier`: on-farm sensor, agricultural network, national service,
model), then within a tier by great-circle distance from the farm pin (nearest first, unknown
last), then by station identity. Each variable of each hour takes the value of the best-ranked
source holding it, and `MergedHour.filled_by` names that source.

- **Parcels**: every parcel of a farm reads the farm's series; its stations are ranked from the
  farm pin.
- **Station identity**: the WIGOS ID when known, else `provider:station id`. Two providers
  serving one station (the replayed and the live Météo-France observations write the same
  station row; `weather_station.wigos_id` is unique, so no two rows share a WIGOS ID) are one
  source, at the best tier either declared. The farm's stored automatic choice
  (`farm.props["weather_sources"]`) is never read: the ranking is recomputed from the pin.
- **Tier**: what the provider declared (`Capabilities.tier`), kept on each receipt
  (`request["tier"]`). A receipt stored before providers declared one reads `LEGACY_TIERS` by
  provider name; a receipt's `endpoint_kind` is audit only and never ranks anything. A source
  with no declared tier ranks after every declared one.
- **No invented hours**: an hour appears only when a source holds a value for it; a variable no
  source holds that hour stays None (never 0, never interpolated).
- **Deterministic**: the result depends on the sources, never on the order they are given in.
- **As of**: reads are bounded by the caller's `[start, end]` on each value's period end, like
  `stations.service.load_hourly`, so a replay at a clock time reads what it read before.

Known limit: a station's (variable, period) holds one value, its first receipt's (`store`), so a
newer release of the same hour (ERA5 replacing ERA5T) is not stored and cannot be merged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from farmcore.core.access import ScopedRepository
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.weather.contract import Point, Tier
from farmcore.core.weather.hourly import HourlyObs
from farmcore.core.weather.selection import great_circle_m
from farmcore.core.weather.variables import BY_LEGACY, VARIABLES

SCOPE = "stations:read"
FIELDS = ("rr1_mm", "t_c", "rh_pct", "ff_kmh")  # the `HourlyObs` values, in its order
PRIORITY = (Tier.ON_FARM, Tier.NETWORK, Tier.NATIONAL, Tier.MODEL)
# Receipts stored before providers declared a tier (the four providers of the time).
LEGACY_TIERS = {
    "meteofrance": Tier.NATIONAL,
    "meteofrance-portal": Tier.NATIONAL,
    "copernicus-era5": Tier.MODEL,
    "open-meteo": Tier.MODEL,
}


@dataclass(frozen=True)
class Source:
    """One station's hours: `hours[ts][field]`, a field it lacks that hour left out."""

    identity: str
    tier: Tier | None
    distance_m: float | None
    hours: Mapping[datetime, Mapping[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class MergedHour:
    ts: datetime
    values: Mapping[str, float | None]  # every field of `FIELDS`
    filled_by: Mapping[str, str]  # field -> the identity of the source of its value

    def obs(self) -> HourlyObs:
        return HourlyObs(ts=self.ts, **self.values)


@dataclass(frozen=True)
class Merged:
    hours: list[MergedHour]
    sources: tuple[Source, ...]  # ranked, best first; only those that filled a value

    def series(self) -> list[HourlyObs]:
        return [h.obs() for h in self.hours]

    def gap_filled(self) -> list[dict[str, Any]]:
        """The values the best source did not fill: per other source and field, its runs of
        consecutive hours as `[first, last]` ISO times. Empty with a single source."""
        if len(self.sources) < 2:
            return []
        out = []
        for source in self.sources[1:]:
            for name in FIELDS:
                runs: list[list[datetime]] = []
                for i, hour in enumerate(self.hours):
                    if hour.filled_by.get(name) != source.identity:
                        continue
                    previous = self.hours[i - 1] if i else None
                    if runs and previous and previous.filled_by.get(name) == source.identity:
                        runs[-1][1] = hour.ts
                    else:
                        runs.append([hour.ts, hour.ts])
                if runs:
                    out.append(
                        {
                            "source": source.identity,
                            "tier": source.tier.value if source.tier else None,
                            "variable": name,
                            "hours": [[a.isoformat(), b.isoformat()] for a, b in runs],
                        }
                    )
        return out


def rank_key(source: Source) -> tuple:
    tier = PRIORITY.index(source.tier) if source.tier in PRIORITY else len(PRIORITY)
    return (tier, source.distance_m is None, source.distance_m or 0.0, source.identity)


def merge(sources: Iterable[Source]) -> Merged:
    """Merge sources, one per station identity (a repeated identity raises `ValueError`)."""
    ranked = sorted(sources, key=rank_key)
    identities = [s.identity for s in ranked]
    if len(set(identities)) != len(identities):
        raise ValueError("one source per station identity")
    hours = []
    used: set[str] = set()
    for ts in sorted({ts for s in ranked for ts in s.hours}):
        values: dict[str, float | None] = dict.fromkeys(FIELDS)
        filled_by: dict[str, str] = {}
        for name in FIELDS:
            for source in ranked:
                value = source.hours.get(ts, {}).get(name)
                if value is not None:
                    values[name], filled_by[name] = value, source.identity
                    break
        if filled_by:
            used.update(filled_by.values())
            hours.append(MergedHour(ts, values, filled_by))
    return Merged(hours, tuple(s for s in ranked if s.identity in used))


def identity(wigos_id: str | None, provider: str, provider_station_id: str) -> str:
    return wigos_id or f"{provider}:{provider_station_id}"


def receipt_tier(provider: str, request: Mapping[str, Any] | None) -> Tier | None:
    declared = (request or {}).get("tier")
    if declared in Tier._value2member_map_:
        return Tier(declared)
    return LEGACY_TIERS.get(provider) if declared is None else None


def farm_hourly(
    repo: ScopedRepository, start: datetime, end: datetime, point: Point | None
) -> Merged:
    """The farm's merged hours with `start <= ts <= end` (`ts`: a value's period end), oldest
    first, from every station it holds observations of through its receipts. `point`: the
    farm pin, for distance within a tier (None: unknown, so identity orders them)."""
    names = [BY_LEGACY[f].name for f in FIELDS]
    obs = repo.list(
        WeatherObservation,
        WeatherObservation.period_end >= start,
        WeatherObservation.period_end <= end,
        WeatherObservation.variable.in_(names),
        scope=SCOPE,
        order_by=(
            WeatherObservation.station_id,
            WeatherObservation.period_end,
            WeatherObservation.variable,
            WeatherObservation.period_start,
            WeatherObservation.id,
        ),
    )
    if not obs:
        return Merged([], ())
    tiers = {
        receipt_id: receipt_tier(provider, request)
        for receipt_id, provider, request in repo.execute(
            select(WeatherReceipt.id, WeatherReceipt.provider, WeatherReceipt.request).where(
                WeatherReceipt.farm_id == repo.farm_id,
                WeatherReceipt.id.in_({o.receipt_id for o in obs}),
            ),
            scope=SCOPE,
            resource="weather_receipt",
        )
    }
    stations = {
        row.id: row
        for row in repo.execute(
            select(
                WeatherStation.id,
                WeatherStation.wigos_id,
                WeatherStation.provider,
                WeatherStation.provider_station_id,
                func.ST_Y(WeatherStation.geom).label("latitude"),
                func.ST_X(WeatherStation.geom).label("longitude"),
            ).where(WeatherStation.id.in_({o.station_id for o in obs})),
            scope=SCOPE,
            resource="weather_station",
        )
    }
    hours: dict[str, dict[datetime, dict[str, float]]] = {}
    best: dict[str, Tier | None] = {}
    distance: dict[str, float | None] = {}
    for o in obs:
        station = stations[o.station_id]
        key = identity(station.wigos_id, station.provider, station.provider_station_id)
        tier = tiers.get(o.receipt_id)
        if key not in best or rank_key(Source(key, tier, None)) < rank_key(
            Source(key, best[key], None)
        ):
            best[key] = tier
        distance[key] = (
            None
            if point is None or station.latitude is None
            else great_circle_m(point, Point(station.latitude, station.longitude))
        )
        hours.setdefault(key, {}).setdefault(o.period_end, {})[
            VARIABLES[o.variable].legacy_name
        ] = float(o.value)
    return merge(Source(key, best[key], distance[key], hours[key]) for key in hours)


def sources_of(merged: Merged) -> Sequence[dict[str, Any]]:
    """The ranked sources that filled the series, as plain data."""
    return [
        {"source": s.identity, "tier": s.tier.value if s.tier else None} for s in merged.sources
    ]
