"""Daily reference evapotranspiration (ET0, the FAO-56 grass reference) at a point.

`daily(repo, first, last, point, as_of, elevation_m)` gives one `DailyEt0` per UTC day
`[D 00:00, D+1 00:00)` (`assumed`: the day is the UTC day, like the official grid's dates):

- **Official first.** A value of `variables.ET0` stored for exactly that day through one of the
  farm's receipts fetched by `as_of` (`store.held`), as a source publishes it (Météo-France's
  daily ETP grid: `weather_meteofrance.etp`), only from the grid cell containing the point
  (`OFFICIAL_HALF_CELL`: the official source is a 0.125 degree grid; a neighbouring cell's value
  is another place's, and an unknown point reads none). Several sources containing the point
  rank as in the merge layer (tier, then distance from the point, then identity).
  Labelled `official` with its provider, dataset, licence and the receipt's attribution.
- **Else computed.** FAO-56 Penman-Monteith (Allen, Pereira, Raes & Smith 1998, FAO Irrigation
  and Drainage Paper 56, eq. 6, G = 0 for a day, eq. 42) by `pyet.pm_fao56` from the merged
  hourly series (`merge.load` with `INPUTS`), the 24 hours ending D 01:00 to D+1 00:00:
  Tmax/Tmin and RHmax/RHmin (eq. 17) are the highest and lowest hourly values; u2 is the mean
  wind converted from 10 m to 2 m (eq. 47: every provider serves wind at the WMO height of 10 m;
  an on-farm sensor's height is not recorded, `assumed` 10 m); Rs is the mean global radiation
  (W m-2) times 0.0864 (MJ m-2 day-1). Latitude is the point's; the site elevation is the
  caller's. Labelled `fao56-pm`; a day ending after `as_of` reads the forecast (`forecast`).
- **Unknown, never zero.** A computed day is unknown (`mm` None, `missing` naming why) when any
  input lacks one of its 24 hours (`assumed`: no hour is filled in or interpolated), or the
  point or the elevation is unknown. A stored zero, or a calm or dark hour, is a value.

Cross-check: on FAO-56 Examples 18 and 20, `pyet` and `refet` (ASCE standardized, short
reference) agree within 0.01 mm/day and reproduce the published values (`tests/core/test_et0.py`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select

from farmcore.core.access import ScopedRepository
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.weather import merge
from farmcore.core.weather.contract import Point, Tier
from farmcore.core.weather.selection import great_circle_m
from farmcore.core.weather.store import held
from farmcore.core.weather.variables import ET0, SHORTWAVE

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
INPUTS = ("t_c", "rh_pct", "ff_kmh", SHORTWAVE)  # merged fields, in `missing` order
NAMES = {"t_c": "temperature", "rh_pct": "humidity", "ff_kmh": "wind", SHORTWAVE: "radiation"}
WIND_HEIGHT_M = 10.0
W_M2_TO_MJ_DAY = 0.0864  # 86 400 s / 10^6
# Half the step of the official grid (Météo-France ETP, 0.125 degree): a cell holds a point
# within this many degrees of its centre in latitude and in longitude.
OFFICIAL_HALF_CELL = 0.0625
OFFICIAL = "official"
COMPUTED = "fao56-pm"


def wind_2m(speed: float, height_m: float = WIND_HEIGHT_M) -> float:
    """FAO-56 eq. 47: wind speed at 2 m from a speed measured at `height_m`."""
    return speed * 4.87 / math.log(67.8 * height_m - 5.42)


@dataclass(frozen=True)
class Inputs:
    """One day's FAO-56 inputs: °C, %, m/s at 2 m, MJ m-2 day-1, degrees, metres."""

    day: date
    tmin: float | None
    tmax: float | None
    rhmin: float | None
    rhmax: float | None
    u2: float | None
    rs: float | None
    latitude: float | None
    elevation_m: float | None

    def complete(self) -> bool:
        return all(
            v is not None and math.isfinite(v)
            for v in (
                self.tmin,
                self.tmax,
                self.rhmin,
                self.rhmax,
                self.u2,
                self.rs,
                self.latitude,
                self.elevation_m,
            )
        )


def penman_monteith(days: Sequence[Inputs]) -> list[float | None]:
    """FAO-56 Penman-Monteith ET0 (mm/day) per day by `pyet`; None for a day whose inputs are
    incomplete or not finite."""
    ready = [d for d in days if d.complete()]
    if not ready:
        return [None] * len(days)
    import pandas as pd  # pyet's own dependency; imported on use (slow to import)
    import pyet

    out: dict[int, float | None] = {}
    # pyet takes one latitude and elevation per call: group the days that share them.
    groups: dict[tuple[float, float], list[Inputs]] = {}
    for d in ready:
        groups.setdefault((d.latitude, d.elevation_m), []).append(d)  # type: ignore[arg-type]
    for (latitude, elevation), group in groups.items():
        index = pd.DatetimeIndex([pd.Timestamp(d.day) for d in group])

        def series(name: str, rows: list[Inputs] = group, idx: Any = index) -> Any:
            return pd.Series([getattr(r, name) for r in rows], index=idx, dtype=float)

        tmax, tmin = series("tmax"), series("tmin")
        result = pyet.pm_fao56(
            (tmax + tmin) / 2,
            series("u2"),
            rs=series("rs"),
            elevation=elevation,
            lat=math.radians(latitude),
            tmax=tmax,
            tmin=tmin,
            rhmax=series("rhmax"),
            rhmin=series("rhmin"),
        )
        for d, value in zip(group, result.tolist(), strict=True):
            out[id(d)] = round(value, 2) if math.isfinite(value) else None
    return [out.get(id(d)) for d in days]


def day_inputs(
    hours: Mapping[datetime, merge.MergedHour],
    day: date,
    latitude: float | None,
    elevation_m: float | None,
) -> tuple[Inputs, tuple[str, ...]]:
    """The day's inputs from merged hours keyed by their end, and the names of those missing
    (an input lacking any of its 24 hours is missing, never filled in)."""
    start = datetime.combine(day, time(), UTC)
    series: dict[str, list[float]] = {}
    for name in INPUTS:
        values = []
        for h in range(1, 25):
            hour = hours.get(start + h * HOUR)
            value = hour.values.get(name) if hour is not None else None
            if value is None or not math.isfinite(value):
                break
            values.append(value)
        if len(values) == 24:
            series[name] = values
    missing = tuple(NAMES[n] for n in INPUTS if n not in series)
    missing += tuple(
        n for n, v in (("latitude", latitude), ("elevation", elevation_m)) if v is None
    )
    t, rh = series.get("t_c"), series.get("rh_pct")
    wind, rad = series.get("ff_kmh"), series.get(SHORTWAVE)
    inputs = Inputs(
        day=day,
        tmin=min(t) if t else None,
        tmax=max(t) if t else None,
        rhmin=min(rh) if rh else None,
        rhmax=max(rh) if rh else None,
        u2=wind_2m(sum(wind) / 24 / 3.6) if wind else None,
        rs=sum(rad) / 24 * W_M2_TO_MJ_DAY if rad else None,
        latitude=latitude,
        elevation_m=elevation_m,
    )
    return inputs, missing


@dataclass(frozen=True)
class DailyEt0:
    day: date
    mm: float | None  # None: unknown, never zero
    method: str | None  # `official`, `fao56-pm`, or None when unknown
    source: str | None  # official: provider/dataset; computed: the method and library
    licence: str | None = None
    attribution: str | None = None
    forecast: bool = False  # the day ends after the read time: computed from the forecast
    missing: tuple[str, ...] = field(default_factory=tuple)  # why an unknown day is unknown

    def as_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "et0_mm": self.mm,
            "method": self.method,
            "source": self.source,
            "licence": self.licence,
            "attribution": self.attribution,
            "forecast": self.forecast,
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class _Official:
    mm: float
    rank: tuple
    source: str
    licence: str
    attribution: str | None


def _official(
    repo: ScopedRepository, start: datetime, end: datetime, point: Point | None, as_of: datetime
) -> dict[date, _Official]:
    """Per day, the best-ranked official ET0 stored for exactly that UTC day."""
    rows = held(
        repo,
        WeatherObservation.variable == ET0,
        WeatherObservation.period_start >= start,
        WeatherObservation.period_end <= end,
        as_of=as_of,
        scope=merge.SCOPE,
        order_by=(WeatherObservation.period_start, WeatherObservation.id),
    )
    if not rows:
        return {}
    receipts = {
        r.id: r
        for r in repo.execute(
            select(
                WeatherReceipt.id,
                WeatherReceipt.provider,
                WeatherReceipt.dataset,
                WeatherReceipt.request,
                WeatherReceipt.licence,
                WeatherReceipt.attribution,
            ).where(
                WeatherReceipt.farm_id == repo.farm_id,
                WeatherReceipt.id.in_({o.receipt_id for o in rows}),
            ),
            scope=merge.SCOPE,
            resource="weather_receipt",
        )
    }
    stations = {
        s.id: s
        for s in repo.execute(
            select(
                WeatherStation.id,
                WeatherStation.wigos_id,
                WeatherStation.provider,
                WeatherStation.provider_station_id,
                func.ST_Y(WeatherStation.geom).label("latitude"),
                func.ST_X(WeatherStation.geom).label("longitude"),
            ).where(WeatherStation.id.in_({o.station_id for o in rows})),
            scope=merge.SCOPE,
            resource="weather_station",
        )
    }
    best: dict[date, _Official] = {}
    for o in rows:
        start_utc = o.period_start.astimezone(UTC)
        if o.period_end - o.period_start != DAY or start_utc.time() != time():
            continue  # not a UTC day's total
        receipt, station = receipts[o.receipt_id], stations[o.station_id]
        tier: Tier | None = merge.receipt_tier(receipt.provider, receipt.request)
        distance = (
            great_circle_m(point, Point(station.latitude, station.longitude))
            if point is not None
            else None
        )
        if point is None or not (
            abs(station.latitude - point.latitude) <= OFFICIAL_HALF_CELL
            and abs(station.longitude - point.longitude) <= OFFICIAL_HALF_CELL
        ):
            continue  # another cell's value, or no point to place it
        ident = merge.identity(station.wigos_id, station.provider, station.provider_station_id)
        candidate = _Official(
            float(o.value),
            merge.rank_key(merge.Source(ident, tier, distance)),
            f"{receipt.provider}/{receipt.dataset}",
            receipt.licence,
            receipt.attribution,
        )
        day = start_utc.date()
        if day not in best or candidate.rank < best[day].rank:
            best[day] = candidate
    return best


def _days(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        day += DAY


def daily(
    repo: ScopedRepository,
    first: date,
    last: date,
    point: Point | None,
    as_of: datetime,
    elevation_m: float | None,
) -> list[DailyEt0]:
    """ET0 for each UTC day from `first` to `last` at `point`, as known at `as_of`."""
    start = datetime.combine(first, time(), UTC)
    end = datetime.combine(last, time(), UTC) + DAY
    official = _official(repo, start, end, point, as_of)
    merged = merge.load(repo, start + HOUR, end, as_of, fields=INPUTS).at(point)
    hours = {h.ts: h for h in merged.hours}
    latitude = point.latitude if point is not None else None
    todo = [
        (day, *day_inputs(hours, day, latitude, elevation_m))
        for day in _days(first, last)
        if day not in official
    ]
    computed = dict(
        zip((d for d, _, _ in todo), penman_monteith([i for _, i, _ in todo]), strict=True)
    )
    missing = {d: m for d, _, m in todo}
    out = []
    for day in _days(first, last):
        ahead = datetime.combine(day, time(), UTC) + DAY > as_of
        if day in official:
            o = official[day]
            out.append(DailyEt0(day, o.mm, OFFICIAL, o.source, o.licence, o.attribution))
        elif computed[day] is not None:
            out.append(DailyEt0(day, computed[day], COMPUTED, _method(), forecast=ahead))
        else:
            out.append(DailyEt0(day, None, None, None, forecast=ahead, missing=missing[day]))
    return out


def _method() -> str:
    from importlib.metadata import version

    return f"FAO-56 Penman-Monteith (pyet {version('pyet')}) from the merged hourly weather"
