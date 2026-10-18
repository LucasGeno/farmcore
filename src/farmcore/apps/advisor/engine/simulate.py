"""Season simulation and the calendar-pass estimate: pure, no DB, clock or I/O.

`simulate(series, config, start, end)` steps the engine once a day at `calendar.eval_hour_local`
(06:00 local) over the local dates [start, end]. When the advice recommends a pass (`spray`, or
`wait` with a next window) and no earlier simulated spray is still ahead, it "sprays" the
recommended products at the first open window (`next_window` start) and feeds that spray back
into later evaluations. A pass counts when its spray falls on or before `end` (local); a
tank-mixed pass counts 2 treatments. Pressure days are evaluation days with a 3-10 event in the
lookback (covered or not: weather pressure, independent of the simulated sprays); window days are
local dates with an open spray window (≥ `window_min_open_hours` open hours) whose run starts in
the daytime band [`calendar.window_day_start_hour_local`, `calendar.window_day_end_hour_local`)
local. Night runs do not count: the hourly window accepts calm night hours, which alone would make
almost every day a window day. In replay terms the windows read observed weather as forecast.
`SeasonResult.threshold` / `source` echo the simulation's own thresholds.

Given `DailyObs` rows (`daily.py`) it runs the daily engine form instead: a pass is sprayed on the
next open day at the evaluation hour, a window day is an open daily window (no daytime band: a
daily row has no hours), and the result is
labelled "daily regional data; 2-calendar-day approximation". Callers (baseline page, KPIs) pass
one series form; one `config` per year (`AdvisorConfig.for_region_year`).

`calendar_passes(config, year)`: a fixed programme over `season_span` (season gate to
`calendar.bunch_closure`), both starting at the gate: downy every
`calendar.downy_interval_rapid_growth_days` while the date is in rapid growth
(`rapid_growth_start` .. `rapid_growth_end`, the engine's protection rule) and every
`calendar.downy_interval_days` otherwise; sulphur every `calendar.sulphur_interval_days`, each
date riding (tank-mixed into) the nearest downy pass within `calendar.merge_days` (earlier on a
tie), else a sulphur-only pass. A tank-mixed pass is one pass, two treatments. Labelled an
estimate: there is no published calendar figure. `CalendarResult.start` / `end` state the span;
`threshold` / `source` echo the intervals and dates used. Compare it with `simulate` over the same
`season_span`.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import daily, incubation, window
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.daily import DailyObs
from farmcore.apps.advisor.engine.decision import evaluate
from farmcore.apps.advisor.engine.downy import RULE as DOWNY_RULE
from farmcore.apps.advisor.engine.gate import gate_start, local_midnight_utc
from farmcore.apps.advisor.engine.types import Advice, HourlyObs, RuleResult, Spray

CALENDAR_LABEL = (
    "estimate: fixed-interval calendar programme, no published figure; counted from the season "
    "gate to bunch closure"
)
HOURLY_LABEL = "hourly station data; replay: observed weather used as forecast"
WINDOW_DAY_LABEL = (
    "window days: an open run (≥ window_min_open_hours) starting in daytime hours (night runs "
    "not counted); days a pass was possible, not how tight the windows were"
)
CALENDAR_THRESHOLDS = (
    "calendar.downy_interval_days",
    "calendar.downy_interval_rapid_growth_days",
    "calendar.sulphur_interval_days",
    "calendar.merge_days",
)
# Dated per year: echoed from the calendar year's own season config.
CALENDAR_SEASON_THRESHOLDS = (
    "season_gate",
    "rapid_growth_start",
    "rapid_growth_end",
    "calendar.bunch_closure",
)
HOURLY_THRESHOLDS = (
    "calendar.eval_hour_local",
    "calendar.window_day_start_hour_local",
    "calendar.window_day_end_hour_local",
    "window_min_open_hours",
)
DAILY_THRESHOLDS = ("calendar.eval_hour_local",)
DAY = timedelta(days=1)
Form = Literal["hourly", "daily"]


@dataclass(frozen=True, slots=True)
class SimPass:
    ts: datetime  # UTC, the start of the window the pass was sprayed in
    day: date  # local date of the spray
    advised_at: datetime  # UTC evaluation time of the advice that triggered it
    decision: str  # "spray" (window open at evaluation) or "wait" (sprayed at the next window)
    products: tuple[str, ...]
    treatments: int
    reasons: tuple[RuleResult, ...]


@dataclass(frozen=True, slots=True)
class SeasonResult:
    """Counts over the simulated local dates.

    `window_days` caveat: with the hourly window's 2-open-hour minimum nearly every day has a
    window, even inside the daytime band (station 33394002, 2024 Apr–Aug: 153 of 153 days; every
    day has ≥ 2 open daytime hours). Read it as "days a pass was possible", not as a measure of
    how tight the windows were; the labels say so.
    """

    form: Form
    start: date
    end: date
    passes: int
    treatments: int
    pressure_days: int
    window_days: int
    days_evaluated: int
    dates: tuple[SimPass, ...]
    labels: tuple[str, ...]
    threshold: dict[str, Any]  # the simulation's own thresholds, as in `RuleResult.threshold`
    source: str


@dataclass(frozen=True, slots=True)
class CalendarPass:
    day: date
    products: tuple[str, ...]  # product names; 2 = one tank-mixed pass, 2 treatments


@dataclass(frozen=True, slots=True)
class CalendarResult:
    year: int
    start: date  # season gate
    end: date  # bunch closure (`calendar.bunch_closure`)
    passes: int
    treatments: int
    dates: tuple[CalendarPass, ...]
    threshold: dict[str, Any]  # the calendar intervals used
    source: str
    label: str = CALENDAR_LABEL


def _every(first: date, last: date, days: float) -> list[date]:
    out: list[date] = []
    step = timedelta(days=days)
    day = first
    while day <= last:
        out.append(day)
        day = first + step * len(out)
    return out


def season_span(config: AdvisorConfig) -> tuple[date, date]:
    """The local dates the calendar programme covers: season gate .. `calendar.bunch_closure`.
    Run `simulate` over the same span to compare model and calendar like for like."""
    return config.season_gate.value, config.calendar.bunch_closure.value


def _downy_dates(season: AdvisorConfig, interval: float, rapid: float) -> list[date]:
    """Downy dates from the gate: each next date is `rapid` days on when the current pass falls
    in rapid growth, else `interval`. The step is chosen from the current pass only, not capped
    at the next phase boundary (2024: 15 Apr → 25 Apr though rapid growth starts 20 Apr), so the
    calendar is slightly conservative (fewer passes): 13 passes / 20 treatments in 2024."""
    gate, end = season_span(season)
    rg_start, rg_end = season.rapid_growth_start.value, season.rapid_growth_end.value
    out: list[date] = []
    day = gate
    while day <= end:
        out.append(day)
        day += timedelta(days=rapid if rg_start <= day <= rg_end else interval)
    return out


def calendar_passes(config: AdvisorConfig, year: int) -> CalendarResult:
    """Calendar passes for `year`; season dates come from `config` when its year matches, else
    from `AdvisorConfig.for_region_year(config.region, year)` (intervals still from `config`)."""
    season = config if config.year == year else AdvisorConfig.for_region_year(config.region, year)
    gate, end = season_span(season)
    cal = config.calendar
    copper = config.default_downy_product.name
    sulphur = config.default_powdery_product.name
    downy = _downy_dates(
        season, cal.downy_interval_days.value, cal.downy_interval_rapid_growth_days.value
    )
    passes: dict[date, list[str]] = {day: [copper] for day in downy}
    merge = timedelta(days=cal.merge_days.value)
    loose: list[date] = []
    for day in _every(gate, end, cal.sulphur_interval_days.value):
        near = min(downy, key=lambda d: (abs(d - day), d))
        if abs(near - day) <= merge and sulphur not in passes[near]:
            passes[near].append(sulphur)
        else:
            loose.append(day)
    dates = sorted(
        [CalendarPass(day, tuple(products)) for day, products in passes.items()]
        + [CalendarPass(day, (sulphur,)) for day in loose],
        key=lambda p: (p.day, len(p.products)),
    )
    return CalendarResult(
        year=year,
        start=gate,
        end=end,
        passes=len(dates),
        treatments=sum(len(p.products) for p in dates),
        dates=tuple(dates),
        threshold={
            **config.thresholds(*CALENDAR_THRESHOLDS),
            **season.thresholds(*CALENDAR_SEASON_THRESHOLDS),
        },
        source="; ".join(
            dict.fromkeys(
                [
                    config.sources(*CALENDAR_THRESHOLDS),
                    season.sources(*CALENDAR_SEASON_THRESHOLDS),
                ]
            )
        ),
    )


def _days(start: date, end: date) -> list[date]:
    return [start + DAY * i for i in range((end - start).days + 1)]


def _local(day: date, hour: int, tz: str) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def _has_pressure(advice: Advice) -> bool:
    downy = advice.reason(DOWNY_RULE)
    return bool(downy and downy.inputs.get("recent_events"))


class _Hourly:
    """Sorted hourly rows with bounded slices, so each daily evaluation reads only what it needs."""

    form: Form = "hourly"
    labels = (HOURLY_LABEL, WINDOW_DAY_LABEL)
    thresholds = HOURLY_THRESHOLDS

    def __init__(self, series: Sequence[HourlyObs], config: AdvisorConfig) -> None:
        self.rows = sorted(series, key=lambda o: o.ts)
        self.ts = [o.ts for o in self.rows]
        self.config = config
        lookback = timedelta(hours=config.downy_window_h.value)
        bloom = local_midnight_utc(config.powdery_start.value, config.local_tz)
        self.history_start = min(gate_start(config), bloom) - lookback
        self.span = window.forecast_span(config)

    def _slice(self, after: datetime, until: datetime) -> list[HourlyObs]:
        return self.rows[bisect_left(self.ts, after) : bisect_right(self.ts, until)]

    def advise(self, sprays: Sequence[Spray], now: datetime) -> Advice:
        return evaluate(self._slice(self.history_start, now + self.span), sprays, self.config, now)

    def window_day(self, day: date) -> bool:
        """An open run starting in the day's daytime band (see the module docstring)."""
        cal, tz = self.config.calendar, self.config.local_tz
        start = _local(day, cal.window_day_start_hour_local.value, tz)
        end = _local(day, cal.window_day_end_hour_local.value, tz)
        scan = window.spray_window(self._slice(start, start + self.span), self.config, start)
        return scan.next_window is not None and scan.next_window[0] < end


class _Daily:
    form: Form = "daily"
    labels = (daily.DAILY_LABEL,)
    thresholds = DAILY_THRESHOLDS

    def __init__(
        self, series: Sequence[DailyObs], config: AdvisorConfig, start: date, end: date
    ) -> None:
        # Keep only the days this season reads (a 10-year series is passed once per year): the
        # 3-10 window before the gate or `start`, through the window horizon after `end`.
        first = min(start, config.season_gate.value) - DAY * config.daily.downy_days.value
        last = end + DAY * (math.ceil(config.window_horizon_h.value / 24) + 1)
        rows = [o for o in series if first <= o.date <= last]
        self.series = daily.DailySeries(rows, config)
        self.config = config

    def advise(self, sprays: Sequence[Spray], now: datetime) -> Advice:
        return daily.evaluate_daily(self.series, sprays, self.config, now)

    def window_day(self, day: date) -> bool:
        return daily.day_open(self.series, day)


def simulate(
    series: Sequence[HourlyObs] | Sequence[DailyObs],
    config: AdvisorConfig,
    start: date,
    end: date,
) -> SeasonResult:
    """Simulate the local dates [start, end] on hourly rows or, given `DailyObs`, on the daily
    engine form. A mixed series is a `ValueError`."""
    kinds = {type(o) for o in series}
    if len(kinds) > 1:
        raise ValueError(
            f"simulate: one series form at a time, got {sorted(k.__name__ for k in kinds)}"
        )
    engine: _Hourly | _Daily
    if kinds == {DailyObs}:
        engine = _Daily(cast(Sequence[DailyObs], series), config, start, end)
    else:
        engine = _Hourly(cast(Sequence[HourlyObs], series), config)
    tz = config.local_tz
    hour = config.calendar.eval_hour_local.value
    end_utc = local_midnight_utc(end + DAY, tz)
    sprays: list[Spray] = []
    passes: list[SimPass] = []
    pressure = windows = 0
    days = _days(start, end)
    for day in days:
        now = _local(day, hour, tz)
        advice = engine.advise(sprays, now)
        pressure += _has_pressure(advice)
        windows += engine.window_day(day)
        pending = any(s.ts > now for s in sprays)
        if not advice.passes or advice.next_window is None or pending:
            continue
        spray = Spray(ts=advice.next_window[0], products=advice.products_recommended)
        sprays.append(spray)
        if spray.ts < end_utc:
            passes.append(
                SimPass(
                    ts=spray.ts,
                    day=spray.ts.astimezone(ZoneInfo(tz)).date(),
                    advised_at=now,
                    decision=advice.decision,
                    products=tuple(p.name for p in spray.products),
                    treatments=advice.treatments,
                    reasons=tuple(advice.reasons),
                )
            )
    return SeasonResult(
        form=engine.form,
        start=start,
        end=end,
        passes=len(passes),
        treatments=sum(p.treatments for p in passes),
        pressure_days=pressure,
        window_days=windows,
        days_evaluated=len(days),
        dates=tuple(passes),
        labels=engine.labels,
        threshold=config.thresholds(*engine.thresholds),
        source=config.sources(*engine.thresholds),
    )


@dataclass(frozen=True, slots=True)
class FirstAlert:
    """The first evaluation day with an uncovered active downy risk, with no spray fed back."""

    day: date  # local date of the 06:00 evaluation that first reads `risk`
    event: datetime | None  # UTC hour of the earliest 3-10 event behind it
    symptoms_at: datetime | None  # the Goidanich table's crossing for that event, if reached


def first_downy_alert(
    series: Sequence[HourlyObs], config: AdvisorConfig, start: date, end: date
) -> FirstAlert | None:
    """Step the engine daily over [start, end] with no sprays; return the first downy `risk`.

    Pure: the same evaluation as `simulate`, minus the feedback of simulated sprays, so the
    answer is when weather first made downy infection a live risk. `symptoms_at` is the
    incubation table's own crossing for the earliest event (None when not reached in the data).
    """
    engine = _Hourly(series, config)
    hour = config.calendar.eval_hour_local.value
    for day in _days(start, end):
        now = _local(day, hour, config.local_tz)
        advice = engine.advise([], now)
        downy = advice.reason(DOWNY_RULE)
        if downy is None or downy.verdict != "risk":
            continue
        incub = advice.reason(incubation.RULE)
        events = (incub.inputs.get("events") if incub else None) or []
        event = min((e["event"] for e in events), default=None)
        symptoms_at = None
        if event is not None:
            upto = min(engine.rows[-1].ts, event + timedelta(days=60))
            symptoms_at = incubation.progress(engine.rows, event, config, upto).symptoms_at
        return FirstAlert(day, event, symptoms_at)
    return None
