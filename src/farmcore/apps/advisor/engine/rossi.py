"""Downy mildew primary infections: the Rossi et al. 2008 model, hour by hour (never daily).

From 1 January every hour adds hydro-thermal time (HT) when it is above 0 °C and the leaf litter
is moist (M: rain, or VPD at or below a threshold); the Gompertz curve DOR(HT) is the share of the
oospores that broke dormancy, and the primary inoculum season (PIS) runs between two HT values.
Inside the PIS each wet event (≥ 0.2 mm rain in its first hour, lasting through the wet hours
after it) starts one cohort, which then goes through the paper's stages:

- germination (GER, the same moist hydro-thermal rate summed from the event's first hour to 1);
- sporangia survival (SUS, from germination; the sporangia die once it passes 1);
- zoospore release (REL: the current wet period, from germination, reaches the hours its mean
  temperature needs; a dry hour restarts the count), before the sporangia die;
- zoospore survival (they die at the first dry hour after release, Eq. 7) and dispersal (rain
  while they survive, the release hour included);
- infection (from dispersal, wet hours × their mean temperature reach the degree-hours; a dry
  hour first kills the zoospores);
- incubation (two rates, the symptoms window's start and end).

"Wet" is leaf wetness W (the paper's measured 0/1 hours): a sensor's minutes when given, else
the relative-humidity estimate of `core/agro/leaf_wetness`, or rain. All parameters come from
`mildew/downy_rossi.py` (the ModelSpec).

**Missing hours.** Hours are read on a dense UTC grid from the season start. An hour with no row,
or missing temperature, humidity or rain, is `missing`: every stage pauses through it. It adds no
HT, germination, survival or incubation, starts no event, is neither wet nor dry (it neither
counts toward release or infection nor resets or kills), and disperses nothing. A run with
missing hours is `complete=False` and its dates may be late or absent; each trace step counts
the missing hours paused through since the step before it.

**Trace.** Every stage a cohort reaches is a `Step` with the hour's inputs, each input's source
(`sources`, from the merged series: station identity and tier) and whether its wetness was
measured or estimated.

**Readings.** `PAPER` is the only reading FarmCore uses. `Reading` exists to explain where an
oracle differs: viticolR reads wetness as litter moisture M for release and infection and
counts release over all hours since germination (a cumulative M count and mean temperature),
where the paper resets at each dry hour (`tests/apps/advisor/test_rossi.py`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from farmcore.apps.advisor.engine.mildew.downy_rossi import P
from farmcore.core.agro.leaf_wetness import leaf_wetness
from farmcore.core.weather.hourly import HourlyObs, aware_utc, floor_hour

HOUR = timedelta(hours=1)
FIELDS = ("t_c", "rh_pct", "rr1_mm")


@dataclass(frozen=True, slots=True)
class Reading:
    """How the stages read wetness: `leaf` (W) or `litter` (M) for release and infection, and
    release counted per wet period (`wet_period`, the paper) or over all hours since germination
    (`cumulative`)."""

    release_wetness: str = "leaf"
    release_window: str = "wet_period"
    infection_wetness: str = "leaf"


PAPER = Reading()


@dataclass(frozen=True, slots=True)
class Step:
    """One stage reached, with the hour's inputs and where each came from."""

    stage: str
    ts: datetime
    t_c: float | None
    rh_pct: float | None
    rr1_mm: float | None
    wetness: str  # measured | estimated | missing
    sources: Mapping[str, str] = field(default_factory=dict)  # input -> source (tier)
    missing_before: int = 0  # missing hours paused through since the previous step


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
    trace: tuple[Step, ...] = ()


@dataclass(frozen=True, slots=True)
class RossiRun:
    cohorts: tuple[Cohort, ...]
    ht: float  # hydro-thermal time at the last hour read
    season: str  # PIS: "not started" | "open" | "ended"
    hours: int  # hours on the grid from the season start to now
    missing_hours: int  # of those, hours with no row or a variable missing
    wetness: str  # measured | estimated (any estimated hour)
    sources: tuple[str, ...] = ()  # every input source the run read, in first-use order

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
    """Share of the season's oospores out of dormancy at hydro-thermal time `ht` (Eq. 2)."""
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
    missing: bool
    wet: bool  # leaf wetness W
    moist: bool  # litter moisture M
    moist_rate: float  # HT/GER increment; 0 when missing, cold or dry
    wetness: str  # measured | estimated | missing
    sources: Mapping[str, str]


def _grid(
    past: Sequence[HourlyObs],
    start: datetime,
    now: datetime,
    measured: Mapping[datetime, float] | None,
    sources: Mapping[datetime, Mapping[str, str]] | None,
) -> list[_Hour]:
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
    for ts, w in zip(ts_list, wet_rows, strict=True):
        o = by_ts.get(ts)
        t, rh, rain = (o.t_c, o.rh_pct, o.rr1_mm) if o else (None, None, None)
        src = dict((sources or {}).get(ts, {}))
        if t is None or rh is None or rain is None:
            hours.append(_Hour(ts, t, rh, rain, True, False, False, 0.0, "missing", src))
            continue
        wet = (w.wet_minutes is not None and w.wet_minutes >= wet_min) or rain >= rain_min
        moist = rain > 0 or saturation_vapour_pressure(t) * (1 - rh / 100) <= vpd_max
        rate = _poly_rate(ht_poly, t) if moist and t > 0 else 0.0
        hours.append(_Hour(ts, t, rh, rain, False, wet, moist, rate, w.origin, src))
    return hours


class _Tracer:
    def __init__(self, hours: list[_Hour]) -> None:
        self.hours = hours
        self.steps: list[Step] = []
        self.last = -1

    def step(self, stage: str, h: int) -> datetime:
        x = self.hours[h]
        lo = self.last + 1 if self.last >= 0 else h
        paused = sum(1 for i in range(lo, h) if self.hours[i].missing)
        self.steps.append(Step(stage, x.ts, x.t, x.rh, x.rain, x.wetness, x.sources, paused))
        self.last = max(self.last, h)
        return x.ts


def _cohort(hours: list[_Hour], e: int, reading: Reading) -> Cohort:
    n = len(hours)
    tr = _Tracer(hours)
    out: dict = {"start": tr.step("start", e)}

    def done() -> Cohort:
        return Cohort(**out, trace=tuple(tr.steps))

    def wet_for(x: _Hour, kind: str) -> bool:
        return x.moist if kind == "litter" else x.wet

    ger, phi = 0.0, None
    for h in range(e, n):
        ger += hours[h].moist_rate
        if ger >= 1:
            phi = h
            break
    if phi is None:
        return done()
    out["germinated"] = tr.step("germinated", phi)

    sus, death = 0.0, n
    poly = P["sporangia_poly"].value
    for h in range(phi, n):
        x = hours[h]
        if x.missing:
            continue
        sus += _poly_rate(poly, x.t * (1 - x.rh / 100)) / 24
        if sus > 1:
            death = h
            break

    a, b = P["release_exp"].value
    tmin = P["release_tmin_c"].value
    cumulative = reading.release_window == "cumulative"
    wd, tsum, count, rho = 0, 0.0, 0, None
    for h in range(phi, death):
        x = hours[h]
        if x.missing:
            continue
        wet = wet_for(x, reading.release_wetness)
        if cumulative:
            wd += wet
            tsum += x.t
            count += 1
        elif not wet:
            wd, tsum, count = 0, 0.0, 0
            continue
        else:
            wd += 1
            tsum += x.t
            count += 1
        twd = tsum / count
        if wd > 0 and twd > tmin and wd >= math.exp((a + b) / twd):
            rho = h
            break
    if rho is None:
        if death < n:
            out["sporangia_dead"] = tr.step("sporangia_dead", death)
        return done()
    out["released"] = tr.step("released", rho)

    dispersal_min = P["dispersal_rain_min_mm"].value
    delta = None
    for h in range(rho, n):
        x = hours[h]
        if x.missing:
            continue
        if h > rho and not x.wet:
            out["zoospores_dead"] = tr.step("zoospores_dead", h)
            break
        if x.rain >= dispersal_min:
            delta = h
            break
    if delta is None:
        return _with_sporangia(out, tr, death, n, done)
    out["dispersed"] = tr.step("dispersed", delta)

    needed = P["infection_degree_hours"].value
    tsum, iota = 0.0, None  # WD·TWD: the sum of the wet hours' temperatures
    for h in range(delta, n):
        x = hours[h]
        if x.missing:
            continue
        if not wet_for(x, reading.infection_wetness):
            out["zoospores_dead"] = tr.step("zoospores_dead", h)
            break
        tsum += x.t
        if tsum >= needed:
            iota = h
            break
    if iota is None:
        return _with_sporangia(out, tr, death, n, done)
    out["infection"] = tr.step("infection", iota)

    lower, upper = P["incubation_poly"].value
    inc_lo = inc_hi = 0.0
    for h in range(iota, n):
        x = hours[h]
        if x.missing:
            continue
        inc_lo += _poly_rate(lower, x.t) / 24
        inc_hi += _poly_rate(upper, x.t) / 24
        if "symptoms_from" not in out and inc_lo >= 1:
            out["symptoms_from"] = tr.step("symptoms_from", h)
        if inc_hi >= 1:
            out["symptoms_to"] = tr.step("symptoms_to", h)
            break
    return _with_sporangia(out, tr, death, n, done)


def _with_sporangia(out: dict, tr: _Tracer, death: int, n: int, done) -> Cohort:
    """The sporangia's death hour, after the zoospore stages (they die on their own clock)."""
    if death < n:
        out["sporangia_dead"] = tr.hours[death].ts
    return done()


def run(
    past: Sequence[HourlyObs],
    start: datetime,
    now: datetime,
    *,
    measured: Mapping[datetime, float] | None = None,
    sources: Mapping[datetime, Mapping[str, str]] | None = None,
    reading: Reading = PAPER,
) -> RossiRun:
    """The model on `past` (any order) from `start` (the season start, 1 January) to `now`.

    `measured`: a leaf-wetness sensor's wet minutes per hour, winning over humidity. `sources`:
    per hour, each input's source label (merged series provenance), carried into the trace."""
    start = floor_hour(aware_utc(start, "start"))
    now = floor_hour(aware_utc(now, "now"))
    hours = _grid(past, start, now, measured, sources)
    pis_start, pis_end = P["pis_ht"].value
    event_min = P["event_rain_min_mm"].value
    ht = 0.0
    hts = []
    for x in hours:
        ht += x.moist_rate
        hts.append(ht)
    cohorts: list[Cohort] = []
    i, n = 0, len(hours)
    while i < n:
        x = hours[i]
        if not x.missing and x.rain >= event_min:
            if pis_start <= hts[i] <= pis_end:
                cohorts.append(_cohort(hours, i, reading))
            i += 1
            while i < n and (hours[i].wet or hours[i].missing):
                i += 1
        else:
            i += 1
    season = "not started" if ht < pis_start else ("open" if ht <= pis_end else "ended")
    missing = sum(x.missing for x in hours)
    estimated = any(x.wetness != "measured" for x in hours) or not hours
    used = dict.fromkeys(s for x in hours for s in x.sources.values())
    return RossiRun(
        tuple(cohorts),
        ht,
        season,
        n,
        missing,
        "estimated" if estimated else "measured",
        tuple(used),
    )
