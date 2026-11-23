"""One hourly series per parcel from every weather source its farm holds (spec W4).

Each station whose observations the farm holds through its own receipts is a source, one per
station identity and tier. Sources rank by tier (`contract.Tier`: on-farm sensor, agricultural
network, national service, model), then within a tier by great-circle distance from the point
read for (nearest first, unknown last), then by station identity. Each variable of each hour
takes the value of the best-ranked source holding it; `MergedHour.filled_by` and
`MergedHour.tiers` name that source and the tier of the value.

- **Parcels**: `load` reads the farm's sources once; `Held.at(point)` ranks them from one
  point. The advisor reads each parcel from its own centroid; the farm pin is the fallback for a
  parcel whose geometry has no centroid (an empty geometry), and the farm-level availability
  check reads from the pin.
- **Station identity**: the WIGOS ID when known, else `provider:station id`. A second provider
  serving a station with a WIGOS ID already held writes its values to that station's row
  (`store.upsert_station`), so the station is one identity. The farm's stored automatic choice
  (`farm.props["weather_sources"]`) is never read: the ranking is recomputed.
- **Tier**: what the provider declared (`Capabilities.tier`), kept on each receipt
  (`request["tier"]`) and so with each value. A receipt stored before providers declared one
  reads `LEGACY_TIERS` by provider name; `endpoint_kind` is audit only and never ranks
  anything. Values of one station from receipts of different tiers are separate sources (each at
  its own tier), so an hour is labelled with the tier of the value that filled it. A source with
  no declared tier ranks after every declared one.
- **Forecast** (model tier, last): the farm's latest forecast receipt fetched by the read time
  (`as_of`) fills, in the hours after `as_of`, each variable no observation holds that hour; it
  never replaces an observed value, never fills a past hour, and adds nothing to an hour it
  lacks.
- **No invented hours**: an hour appears only when a source holds a value for it; a variable no
  source holds that hour stays None (never 0, never interpolated).
- **Deterministic**: the result depends on the sources, never on the order they are given in.
- **Fields**: `FIELDS` (the `HourlyObs` values) unless the caller names others (`load(...,
  fields=...)`): a variable without a legacy name (global radiation, for ET0) is merged under
  its CF name, by the same rules. `MergedHour.obs()` needs the default fields.
- **As of**: values are bounded by the caller's `[start, end]` on their period end (the value
  horizon, like `stations.service.load_hourly`), and separately by receipt: only receipts
  fetched by `as_of` are read, so a later backfill or late ingest is not seen by a replay of an
  earlier time. A provisional value (ERA5T) gives way to the final one for the same station,
  variable and period once a receipt fetched by `as_of` holds it (`store.held`), so the
  current read takes the final value and a replay of an earlier time the provisional one.

Known limit: observations are visible only through the receipts of the farm that stored them;
a conflicting write by a second farm to a shared station raises (`store`). ERA5 grid points
are per farm, so two farms in one cell never conflict.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from farmcore.core.access import ScopedRepository
from farmcore.core.models.weather import (
    WeatherForecast,
    WeatherObservation,
    WeatherReceipt,
    WeatherStation,
)
from farmcore.core.weather.contract import Point, Tier
from farmcore.core.weather.hourly import HourlyObs
from farmcore.core.weather.selection import great_circle_m
from farmcore.core.weather.store import held
from farmcore.core.weather.variables import BY_LEGACY, VARIABLES

SCOPE = "stations:read"
FIELDS = ("rr1_mm", "t_c", "rh_pct", "ff_kmh")  # the `HourlyObs` values, in its order
PRIORITY = (Tier.ON_FARM, Tier.NETWORK, Tier.NATIONAL, Tier.MODEL)
HOUR = timedelta(hours=1)
# Receipts stored before providers declared a tier (the four providers of the time).
LEGACY_TIERS = {
    "meteofrance": Tier.NATIONAL,
    "meteofrance-portal": Tier.NATIONAL,
    "copernicus-era5": Tier.MODEL,
    "open-meteo": Tier.MODEL,
}


@dataclass(frozen=True)
class Source:
    """One station's hours at one tier: `hours[ts][field]`, a field it lacks that hour left
    out."""

    identity: str
    tier: Tier | None
    distance_m: float | None
    hours: Mapping[datetime, Mapping[str, float]] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, Tier | None]:
        return (self.identity, self.tier)


@dataclass(frozen=True)
class MergedHour:
    ts: datetime
    values: Mapping[str, float | None]  # every merged field (`FIELDS` by default)
    filled_by: Mapping[str, str]  # field -> the identity of the source of its value
    tiers: Mapping[str, Tier | None] = field(default_factory=dict)  # field -> that value's tier

    def obs(self) -> HourlyObs:
        return HourlyObs(ts=self.ts, **self.values)


@dataclass(frozen=True)
class Merged:
    hours: list[MergedHour]
    sources: tuple[Source, ...]  # ranked, best first (the forecast last); only those used
    fields: tuple[str, ...] = FIELDS

    def series(self) -> list[HourlyObs]:
        return [h.obs() for h in self.hours]

    def provenance(self) -> list[dict[str, int]]:
        """Per hour, each filled field's source as its index in `sources`."""
        index = {s.key: i for i, s in enumerate(self.sources)}
        return [
            {name: index[(hour.filled_by[name], hour.tiers.get(name))] for name in hour.filled_by}
            for hour in self.hours
        ]

    def filled(self) -> list[dict[str, Any]]:
        """Per source (ranked) and field, the runs of consecutive hours whose value it gave, as
        `[first, last]` ISO times; a run ends where the next hour is missing or filled by
        another source."""
        out = []
        for source in self.sources:
            for name in self.fields:
                runs: list[list[datetime]] = []
                for hour in self.hours:
                    if (hour.filled_by.get(name), hour.tiers.get(name)) != source.key:
                        continue
                    if runs and runs[-1][1] + HOUR == hour.ts:
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

    def gap_filled(self) -> list[dict[str, Any]]:
        """`filled` without the best source: the values it did not give. Empty with a single
        source."""
        if len(self.sources) < 2:
            return []
        best = self.sources[0]
        return [
            f
            for f in self.filled()
            if (f["source"], f["tier"]) != (best.identity, best.tier.value if best.tier else None)
        ]


def rank_key(source: Source) -> tuple:
    tier = PRIORITY.index(source.tier) if source.tier in PRIORITY else len(PRIORITY)
    return (tier, source.distance_m is None, source.distance_m or 0.0, source.identity)


def merge(
    sources: Iterable[Source],
    forecast: Source | None = None,
    after: datetime | None = None,
    fields: tuple[str, ...] = FIELDS,
) -> Merged:
    """Merge sources, one per station identity and tier (a repeated one raises `ValueError`).
    `forecast` fills, after `after`, only the variables no source holds that hour."""
    ranked = sorted(sources, key=rank_key)
    keys = [s.key for s in ranked]
    if len(set(keys)) != len(keys):
        raise ValueError("one source per station identity and tier")
    hours = []
    used: set[tuple[str, Tier | None]] = set()
    for ts in sorted({ts for s in ranked for ts in s.hours}):
        values: dict[str, float | None] = dict.fromkeys(fields)
        filled_by: dict[str, str] = {}
        tiers: dict[str, Tier | None] = {}
        for name in fields:
            for source in ranked:
                value = source.hours.get(ts, {}).get(name)
                if value is not None:
                    values[name], filled_by[name], tiers[name] = value, *source.key
                    used.add(source.key)
                    break
        if filled_by:
            hours.append(MergedHour(ts, values, filled_by, tiers))
    if forecast is not None and after is not None:
        observed = {h.ts: i for i, h in enumerate(hours)}
        for ts in sorted(forecast.hours):
            held = hours[observed[ts]] if ts in observed else None
            row = {
                n: v
                for n, v in forecast.hours[ts].items()
                if n in fields and v is not None and (held is None or held.values[n] is None)
            }
            if ts <= after or not row:
                continue
            merged = MergedHour(
                ts,
                {**(held.values if held else dict.fromkeys(fields)), **row},
                {**(held.filled_by if held else {}), **dict.fromkeys(row, forecast.identity)},
                {**(held.tiers if held else {}), **dict.fromkeys(row, forecast.tier)},
            )
            if held is None:
                hours.append(merged)
            else:
                hours[observed[ts]] = merged
            used.add(forecast.key)
        hours.sort(key=lambda h: h.ts)
        ranked.append(forecast)
    return Merged(hours, tuple(s for s in ranked if s.key in used), fields)


def cf_name(field_name: str) -> str:
    """The CF variable a merged field reads: its legacy name's, or the field is the CF name."""
    return BY_LEGACY[field_name].name if field_name in BY_LEGACY else field_name


def field_of(variable: str) -> str:
    """The field a stored CF variable is merged under: its legacy name, else its CF name."""
    return VARIABLES[variable].legacy_name or variable


def identity(wigos_id: str | None, provider: str, provider_station_id: str) -> str:
    return wigos_id or f"{provider}:{provider_station_id}"


def receipt_tier(provider: str, request: Mapping[str, Any] | None) -> Tier | None:
    declared = (request or {}).get("tier")
    if declared in Tier._value2member_map_:
        return Tier(declared)
    return LEGACY_TIERS.get(provider) if declared is None else None


@dataclass(frozen=True)
class HeldStation:
    identity: str
    latitude: float | None
    longitude: float | None
    hours: Mapping[Tier | None, Mapping[datetime, Mapping[str, float]]]  # per value tier


@dataclass(frozen=True)
class Held:
    """A farm's weather as read at `as_of`, not yet ranked from a point."""

    stations: tuple[HeldStation, ...]
    forecast: Source | None
    as_of: datetime
    fields: tuple[str, ...] = FIELDS

    def __bool__(self) -> bool:
        return bool(self.stations) or self.forecast is not None

    def at(self, point: Point | None) -> Merged:
        """The merged series ranked from `point` (None: distance unknown, identity orders)."""

        def distance(s: HeldStation) -> float | None:
            if point is None or s.latitude is None:
                return None
            return great_circle_m(point, Point(s.latitude, s.longitude))

        return merge(
            (
                Source(s.identity, tier, distance(s), hours)
                for s in self.stations
                for tier, hours in s.hours.items()
            ),
            self.forecast,
            self.as_of,
            self.fields,
        )


def load(
    repo: ScopedRepository,
    start: datetime,
    end: datetime,
    as_of: datetime,
    fields: tuple[str, ...] = FIELDS,
) -> Held:
    """The farm's values with `start <= ts <= end` (`ts`: a value's period end, or a
    forecast's valid time) from the receipts it fetched by `as_of`."""
    names = [cf_name(f) for f in fields]
    obs = held(
        repo,
        WeatherObservation.period_end >= start,
        WeatherObservation.period_end <= end,
        WeatherObservation.variable.in_(names),
        as_of=as_of,
        scope=SCOPE,
        order_by=(
            WeatherObservation.station_id,
            WeatherObservation.period_end,
            WeatherObservation.variable,
            WeatherObservation.period_start,
            WeatherObservation.id,
        ),
    )
    stations: tuple[HeldStation, ...] = ()
    if obs:
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
        rows = {
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
        hours: dict[str, dict[Tier | None, dict[datetime, dict[str, float]]]] = {}
        where: dict[str, Any] = {}
        for o in obs:
            row = rows[o.station_id]
            key = identity(row.wigos_id, row.provider, row.provider_station_id)
            where.setdefault(key, row)
            hours.setdefault(key, {}).setdefault(tiers.get(o.receipt_id), {}).setdefault(
                o.period_end, {}
            )[field_of(o.variable)] = float(o.value)
        stations = tuple(
            HeldStation(key, where[key].latitude, where[key].longitude, hours[key])
            for key in sorted(hours)
        )
    return Held(stations, _forecast(repo, end, as_of, names), as_of, fields)


def _forecast(
    repo: ScopedRepository, end: datetime, as_of: datetime, names: list[str]
) -> Source | None:
    """The latest forecast receipt fetched by `as_of` (member 0), its hours in (as_of, end]."""
    latest = repo.execute(
        select(
            WeatherReceipt.id,
            WeatherReceipt.provider,
            WeatherReceipt.dataset,
            WeatherReceipt.request,
        )
        .where(
            WeatherReceipt.farm_id == repo.farm_id,
            WeatherReceipt.fetched_at <= as_of,
            exists().where(WeatherForecast.receipt_id == WeatherReceipt.id),
        )
        .order_by(WeatherReceipt.fetched_at.desc(), WeatherReceipt.id.desc())
        .limit(1),
        scope=SCOPE,
        resource="weather_receipt",
    ).first()
    if latest is None:
        return None
    hours: dict[datetime, dict[str, float]] = {}
    for v in repo.list(
        WeatherForecast,
        WeatherForecast.receipt_id == latest.id,
        WeatherForecast.member == 0,
        WeatherForecast.valid_time > as_of,
        WeatherForecast.valid_time <= end,
        WeatherForecast.variable.in_(names),
        scope=SCOPE,
        order_by=(
            WeatherForecast.valid_time,
            WeatherForecast.variable,
            WeatherForecast.model,
            WeatherForecast.id,
        ),
    ):
        hours.setdefault(v.valid_time, {}).setdefault(field_of(v.variable), float(v.value))
    if not hours:
        return None
    return Source(
        f"{latest.provider}:{latest.dataset}",
        receipt_tier(latest.provider, latest.request),
        None,
        hours,
    )


def farm_hourly(
    repo: ScopedRepository,
    start: datetime,
    end: datetime,
    point: Point | None,
    as_of: datetime,
) -> Merged:
    """`load(repo, start, end, as_of).at(point)`."""
    return load(repo, start, end, as_of).at(point)


def has_weather(session: Session, farm_id: int) -> bool:
    """Whether the farm holds any weather value (observation or forecast) through its receipts:
    receipt metadata only, never a value."""
    mine = select(WeatherReceipt.id).where(WeatherReceipt.farm_id == farm_id)
    return bool(
        session.scalar(
            select(
                or_(
                    exists().where(WeatherObservation.receipt_id.in_(mine)),
                    exists().where(WeatherForecast.receipt_id.in_(mine)),
                )
            )
        )
    )


def sources_of(merged: Merged) -> Sequence[dict[str, Any]]:
    """The ranked sources that filled the series, as plain data."""
    return [
        {"source": s.identity, "tier": s.tier.value if s.tier else None} for s in merged.sources
    ]
