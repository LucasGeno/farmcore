"""The graded spray window: the next `DAYS` days, each graded best / good / possible / avoid with
a reason in words. Ordered labels only: no percentages, no score.

Every rule is the advisor's own (`advisor.engine.window.closed_by`, thresholds in
`AdvisorConfig`): an hour is open when the wind, the rain of the hour, the rain in the next hours
and (sulphur only) the temperature pass the spray-window test. Hours are the daytime band of
the advisor's calendar (`calendar.window_day_start_hour_local` to `..._end_hour_local`), from
the current hour on; a day with no band hour left (today after the band) is skipped.

- good: the day's longest run of open hours is at least `window_min_open_hours`;
- best: the good day with the longest run, one at most; equal runs go to less rain in the run,
  then the lower top wind, then the earliest day (an ordering only: the window grades the
  weather, and whether and by when to spray stays the advice's call);
- possible: open hours, but no run that long;
- avoid: no open hour; the reason names what closed the hours;
- no_forecast: the series does not reach the day's last band hour plus the rain lookahead.
- busy: a local day the farmer marked unavailable, excluded before choosing best; weather
  facts and hourly rules remain unchanged.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs
from farmcore.apps.advisor.engine.types import floor_hour
from farmcore.apps.advisor.engine.window import closed_by
from farmcore.apps.stations.conditions import WIND_MARGINAL_KMH

DAYS = 5
GRADES = ("best", "good", "possible", "avoid")  # in order of suitability
NO_FORECAST = "no_forecast"
HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DayGrade:
    day: date  # local date
    grade: str  # one of GRADES, NO_FORECAST or "busy" (availability only)
    reason: str
    open_from: datetime | None = None  # the longest open run, UTC
    open_until: datetime | None = None
    open_hours: int = 0
    hours: tuple[datetime, ...] = ()  # the band hours graded, UTC
    run_rain_mm: float = 0.0  # rain during the longest open run
    run_wind_kmh: float = 0.0  # its top wind
    has_forecast: bool = True  # availability cannot hide missing weather


def _band(day: date, config: AdvisorConfig, zone: ZoneInfo, current: datetime) -> list[datetime]:
    cal = config.calendar
    start, end = cal.window_day_start_hour_local.value, cal.window_day_end_hour_local.value
    hours = (datetime(day.year, day.month, day.day, h, tzinfo=zone) for h in range(start, end))
    return [h.astimezone(current.tzinfo) for h in hours if h >= current]


def _longest_run(hours: list[datetime], closed: dict[datetime, list[str]]) -> list[datetime]:
    best: list[datetime] = []
    run: list[datetime] = []
    for h in hours:
        run = [*run, h] if not closed[h] else []
        if len(run) > len(best):
            best = run
    return best


def _causes(series: Sequence[HourlyObs], hours: list[datetime], closed: dict) -> str:
    by_ts = {o.ts: o for o in series}
    why = {c for h in hours for c in closed[h]}
    parts = []
    if why & {"rain_rate", "rain_next"}:
        rain = sum(o.rr1_mm or 0.0 for o in series if hours[0] < o.ts <= hours[-1] + HOUR)
        parts.append(f"rain {rain:.1f} mm")
    if "wind" in why:
        winds = [by_ts[h].ff_kmh for h in hours if "wind" in closed[h] and h in by_ts]
        known = [w for w in winds if w is not None]
        parts.append(f"wind up to {max(known):.0f} km/h" if known else "no wind forecast")
    if "sulphur_temp" in why:
        temps = [by_ts[h].t_c for h in hours if "sulphur_temp" in closed[h] and h in by_ts]
        known = [t for t in temps if t is not None]
        parts.append(f"too hot for sulphur ({max(known):.0f} °C)" if known else "no temperature")
    if "no_data" in why:
        parts.append("missing forecast hours")
    return ", ".join(parts)


def _grade_day(
    day: date,
    hours: list[datetime],
    series: Sequence[HourlyObs],
    config: AdvisorConfig,
    zone: ZoneInfo,
    last_ts: datetime | None,
    sulphur: bool,
) -> DayGrade:
    lookahead = config.window_rain_lookahead_h.value * HOUR
    if last_ts is None or hours[-1] + lookahead > last_ts:
        return DayGrade(day, NO_FORECAST, "Beyond the forecast", has_forecast=False)
    by_ts = {o.ts: o for o in series}
    rain_hours = {
        h + i * HOUR for h in hours for i in range(config.window_rain_lookahead_h.value + 1)
    }
    if any(h not in by_ts or by_ts[h].rr1_mm is None for h in rain_hours) or any(
        h not in by_ts or by_ts[h].ff_kmh is None or (sulphur and by_ts[h].t_c is None)
        for h in hours
    ):
        return DayGrade(
            day,
            NO_FORECAST,
            "Forecast inputs unavailable: missing required hours",
            has_forecast=False,
        )
    closed = closed_by(series, hours, config, sulphur=sulphur)
    run = _longest_run(hours, closed)
    if not run:
        causes = _causes(series, hours, closed)
        return DayGrade(day, "avoid", causes[:1].upper() + causes[1:])
    start, until = run[0], run[-1] + HOUR
    if len(run) < config.window_min_open_hours.value:
        causes = _causes(series, hours, closed)
        return DayGrade(
            day,
            "possible",
            f"Only short gaps ({len(run)} h open at most): {causes}",
            start,
            until,
            len(run),
        )
    by_ts = {o.ts: o for o in series}
    wind = max((by_ts[h].ff_kmh or 0.0) for h in run)
    local = f"{start.astimezone(zone):%H:%M}–{until.astimezone(zone):%H:%M}"
    # the Weather app's own breeze flag (Beaufort 3 from 12 km/h): words only, same grade
    breezy = "breezy, " if wind >= WIND_MARGINAL_KMH else ""
    # the rain that falls during the run (the window lets light rain through: say how much)
    rain = sum(o.rr1_mm or 0.0 for o in series if start < o.ts <= until)
    wet = f"rain {rain:.1f} mm" if round(rain, 1) else "no rain"
    reason = f"Open {local} ({len(run)} h): {wet}, {breezy}wind up to {wind:.0f} km/h"
    return DayGrade(
        day, "good", reason, start, until, len(run), run_rain_mm=rain, run_wind_kmh=wind
    )


def rank_days(
    series: Sequence[HourlyObs],
    now: datetime,
    tz: str,
    config: AdvisorConfig,
    *,
    days: int = DAYS,
    sulphur: bool = False,
    busy_days: Collection[date] = (),
) -> list[DayGrade]:
    """The next `days` local days with band hours left; busy days cannot be good or best."""
    zone = ZoneInfo(tz)
    current = floor_hour(now)
    last_ts = max((o.ts for o in series), default=None)
    out: list[DayGrade] = []
    day = now.astimezone(zone).date()
    while len(out) < days:
        hours = _band(day, config, zone, current)
        if hours:
            graded = _grade_day(day, hours, series, config, zone, last_ts, sulphur)
            out.append(
                replace(
                    graded, grade="busy" if day in busy_days else graded.grade, hours=tuple(hours)
                )
            )
        day += timedelta(days=1)
    good = [d for d in out if d.grade == "good"]
    if good:
        top = min(good, key=lambda d: (-d.open_hours, d.run_rain_mm, d.run_wind_kmh, d.day))
        out[out.index(top)] = replace(top, grade="best")
    return out
