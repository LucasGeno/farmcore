"""Downy mildew primary infections: the Rossi et al. 2008 model, hour by hour (never daily).

From 1 January every hour adds hydro-thermal time (HT) when it is above 0 °C and moist (rain, or
VPD at or below a threshold); the Gompertz curve DOR(HT) is the share of the oospores that broke
dormancy, and the primary inoculum season (PIS) runs between two HT values. Inside the PIS each
wet event (≥ 0.2 mm rain in its first hour, lasting through the wet hours after it) starts one
cohort, which then goes through the paper's stages:

- germination (GER, the same moist hydro-thermal rate summed from the event's first hour to 1);
- sporangia survival (SUS, from germination; the sporangia die once it passes 1);
- zoospore release (REL: the current wet period, from germination, reaches the hours the mean
  temperature needs; a dry hour restarts the count), before the sporangia die;
- zoospore survival (they die at the first dry hour after release) and dispersal (rain while
  they survive, the release hour included);
- infection (from dispersal, wet hours × their mean temperature reach the degree-hours; a dry
  hour first kills the zoospores);
- incubation (two rates, the symptoms window's start and end).

All parameters come from `mildew/downy_rossi.py` (the ModelSpec). Hours are read on a dense
UTC grid from the season start: an hour with no row, or with a variable missing, is `missing`.
It adds nothing (no HT, no wetness), so a run with missing hours is `complete=False` and its
dates may be late or absent; it is never presented as a complete run. Reads only rows with
ts ≤ `now`. A leaf-wetness sensor's minutes, when given, win over the humidity estimate
(`core/agro/leaf_wetness`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from farmcore.apps.advisor.engine.mildew.downy_rossi import P
from farmcore.core.agro.leaf_wetness import leaf_wetness
from farmcore.core.weather.hourly import HourlyObs, aware_utc, floor_hour

HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Cohort:
    """One wet event's oospores through the stages; None: the stage was not reached by now."""

    start: datetime  # the event's first hour (rain ≥ 0.2 mm)
    germinated: datetime | None = None
    sporangia_dead: datetime | None = None
    released: datetime | None = None
    zoospores_dead: datetime | None = None
    dispersed: datetime | None = None
    infection: datetime | None = None
    symptoms_from: datetime | None = None
    symptoms_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class RossiRun:
    cohorts: tuple[Cohort, ...]
    ht: float  # hydro-thermal time at the last hour read
    season: str  # PIS: "not started" | "open" | "ended"
    hours: int  # hours on the grid from the season start to now
    missing_hours: int  # of those, hours with no row or a variable missing
    wetness: str  # measured | estimated (any estimated hour)

    @property
    def complete(self) -> bool:
        return self.hours > 0 and self.missing_hours == 0

    @property
    def infections(self) -> tuple[Cohort, ...]:
        return tuple(c for c in self.cohorts if c.infection is not None)


def saturation_vapour_pressure(t_c: float) -> float:
    a, b, c = P["vapour_pressure"].value
    return a * math.exp(b * t_c / (c + t_c))


def dor(ht: float) -> float:
    """Share of the season's oospores out of dormancy at hydro-thermal time `ht` (Eq. 3)."""
    a, b = P["dor_gompertz"].value
    return math.exp(-a * math.exp(-b * (ht + 1)))


def _poly_rate(coeffs: tuple[float, float, float], x: float) -> float:
    a, b, c = coeffs
    return 1 / (a + b * x + c * x * x)


@dataclass(frozen=True, slots=True)
class _Hour:
    ts: datetime
    t: float | None
    rh: float | None
    rain: float | None
    wet: bool
    moist_rate: float  # HT/GER increment; 0 when missing, cold or dry


def _grid(
    past: Sequence[HourlyObs],
    start: datetime,
    now: datetime,
    measured: Mapping[datetime, float] | None,
) -> tuple[list[_Hour], int, str]:
    by_ts = {o.ts: o for o in past if start <= o.ts <= now}
    ts_list = []
    ts = start
    while ts <= now:
        ts_list.append(ts)
        ts += HOUR
    sensor = {aware_utc(k, "measured hour"): v for k, v in (measured or {}).items()}
    sensor = {k: v for k, v in sensor.items() if start <= k <= now}
    wet_rows = leaf_wetness(
        ((t, by_ts[t].rh_pct if t in by_ts else None) for t in ts_list), measured=sensor
    )
    wet_min = P["wet_minutes_min"].value
    rain_min = P["event_rain_min_mm"].value
    vpd_max = P["moist_vpd_max_hpa"].value
    ht_poly = P["ht_poly"].value
    hours: list[_Hour] = []
    missing = 0
    estimated = False
    for ts, w in zip(ts_list, wet_rows, strict=True):
        o = by_ts.get(ts)
        t, rh, rain = (o.t_c, o.rh_pct, o.rr1_mm) if o else (None, None, None)
        if t is None or rh is None or rain is None:
            missing += 1
        estimated = estimated or w.origin != "measured"
        wet = (w.wet_minutes is not None and w.wet_minutes >= wet_min) or (
            rain is not None and rain >= rain_min
        )
        rate = 0.0
        if t is not None and t > 0 and (rh is not None or rain is not None):
            vpd = saturation_vapour_pressure(t) * (1 - rh / 100) if rh is not None else None
            if (rain is not None and rain > 0) or (vpd is not None and vpd <= vpd_max):
                rate = _poly_rate(ht_poly, t)
        hours.append(_Hour(ts, t, rh, rain, wet, rate))
    return hours, missing, "estimated" if estimated or not hours else "measured"


def _cohort(hours: list[_Hour], e: int) -> Cohort:
    n = len(hours)
    out = {"start": hours[e].ts}

    def done() -> Cohort:
        return Cohort(**out)

    ger, phi = 0.0, None
    for h in range(e, n):
        ger += hours[h].moist_rate
        if ger >= 1:
            phi = h
            break
    if phi is None:
        return done()
    out["germinated"] = hours[phi].ts

    sus, death = 0.0, n
    poly = P["sporangia_poly"].value
    for h in range(phi, n):
        x = hours[h]
        if x.t is not None and x.rh is not None:
            sus += _poly_rate(poly, x.t * (1 - x.rh / 100)) / 24
        if sus > 1:
            death = h
            out["sporangia_dead"] = x.ts
            break

    a, b = P["release_exp"].value
    tmin = P["release_tmin_c"].value
    wd, tsum, rho = 0, 0.0, None
    for h in range(phi, death):
        x = hours[h]
        if not x.wet or x.t is None:
            wd, tsum = 0, 0.0
            continue
        wd += 1
        tsum += x.t
        twd = tsum / wd
        if twd > tmin and wd >= math.exp((a + b) / twd):
            rho = h
            break
    if rho is None:
        return done()
    out["released"] = hours[rho].ts

    dispersal_min = P["dispersal_rain_min_mm"].value
    delta = None
    for h in range(rho, n):
        x = hours[h]
        if h > rho and not x.wet:
            out["zoospores_dead"] = x.ts
            break
        if x.rain is not None and x.rain >= dispersal_min:
            delta = h
            break
    if delta is None:
        return done()
    out["dispersed"] = hours[delta].ts

    needed = P["infection_degree_hours"].value
    tsum, iota = 0.0, None  # WD·TWD: the sum of the wet hours' temperatures
    for h in range(delta, n):
        x = hours[h]
        if not x.wet or x.t is None:
            out["zoospores_dead"] = x.ts
            break
        tsum += x.t
        if tsum >= needed:
            iota = h
            break
    if iota is None:
        return done()
    out["infection"] = hours[iota].ts

    lower, upper = P["incubation_poly"].value
    inc_lo = inc_hi = 0.0
    for h in range(iota, n):
        t = hours[h].t
        if t is None:
            continue
        inc_lo += _poly_rate(lower, t) / 24
        inc_hi += _poly_rate(upper, t) / 24
        if "symptoms_from" not in out and inc_lo >= 1:
            out["symptoms_from"] = hours[h].ts
        if inc_hi >= 1:
            out["symptoms_to"] = hours[h].ts
            break
    return done()


def run(
    past: Sequence[HourlyObs],
    start: datetime,
    now: datetime,
    *,
    measured: Mapping[datetime, float] | None = None,
) -> RossiRun:
    """The model on `past` (any order) from `start` (the season start, 1 January) to `now`.

    `measured`: a leaf-wetness sensor's wet minutes per hour, winning over humidity."""
    start = floor_hour(aware_utc(start, "start"))
    now = floor_hour(aware_utc(now, "now"))
    hours, missing, wetness = _grid(past, start, now, measured)
    pis_start, pis_end = P["pis_ht"].value
    event_min = P["event_rain_min_mm"].value
    ht = 0.0
    cohorts: list[Cohort] = []
    i, n = 0, len(hours)
    hts = []
    for x in hours:
        ht += x.moist_rate
        hts.append(ht)
    while i < n:
        x = hours[i]
        if x.rain is not None and x.rain >= event_min:
            if pis_start <= hts[i] <= pis_end:
                cohorts.append(_cohort(hours, i))
            i += 1
            while i < n and hours[i].wet:
                i += 1
        else:
            i += 1
    season = "not started" if ht < pis_start else ("open" if ht <= pis_end else "ended")
    return RossiRun(tuple(cohorts), ht, season, n, missing, wetness)
