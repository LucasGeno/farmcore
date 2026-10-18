"""Spray window: the only engine module that reads hours after `now`.

The search runs on the hourly grid: `now` is floored to the UTC hour (the "current hour"), so a
wall-clock `now` such as 12:30 or 12:00:07 reads the 12:00 row, as 12:00 does. Hour h is open
when: ff(h) ≤ wind max; rr1(h) < rain-rate max; the rain over rows with ts ∈ (h, h + lookahead]
(RR1 at hour H covers the hour before H) is complete and sums to < next-rain max; and, for
sulphur, t(h) < sulphur max. Missing values close the hour.

The next window is the first run of ≥ min-open-hours consecutive open hours whose start hours lie
in [current hour, current hour + horizon); its end is the first closed hour and never later than
the horizon. "Open now" means that run starts at the current hour. The last hours of the horizon
read rain up to `forecast_span(config)` after now: a caller that loads fewer future rows gets those
hours closed for lack of data. In replay the future rows are observed weather, so reading any of
them sets `replay_forecast`.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.types import HourlyObs, RuleResult, floor_hour

RULE = "spray_window"
THRESHOLDS = (
    "window_wind_max_kmh",
    "window_rr1_max_mm_h",
    "window_rain_lookahead_h",
    "window_rain_next_max_mm",
    "window_sulphur_tmax_c",
    "window_min_open_hours",
    "window_horizon_h",
)
REPLAY_NOTE = "replay: observed weather used as forecast"
HOUR = timedelta(hours=1)


def forecast_span(config: AdvisorConfig) -> timedelta:
    """How far after `now` the window reads: load rows with ts ≤ now + this to search the full
    horizon (72 h + 2 h lookahead − 1 h with the defaults)."""
    return (config.window_horizon_h.value + config.window_rain_lookahead_h.value - 1) * HOUR


@dataclass(frozen=True, slots=True)
class WindowScan:
    open_now: bool
    next_window: tuple[datetime, datetime] | None
    used_future: bool
    reason: RuleResult


class _Series:
    def __init__(self, series: Sequence[HourlyObs]) -> None:
        self.rows = sorted(series, key=lambda o: o.ts)
        self.ts = [o.ts for o in self.rows]
        self.by_ts = {o.ts: o for o in self.rows}

    def between(self, after: datetime, until: datetime) -> list[HourlyObs]:
        """Rows with ts ∈ (after, until]."""
        return self.rows[bisect_right(self.ts, after) : bisect_right(self.ts, until)]

    def any_in(self, after: datetime, until: datetime) -> bool:
        return bisect_right(self.ts, after) < bisect_right(self.ts, until)


def _hour(
    s: _Series, h: datetime, config: AdvisorConfig, sulphur: bool
) -> tuple[list[str], float | None]:
    """(conditions that close hour h, rain in the next hours or None when incomplete)."""
    closed: list[str] = []
    obs = s.by_ts.get(h)
    if obs is None:
        return ["no_data"], None
    if obs.ff_kmh is None or obs.ff_kmh > config.window_wind_max_kmh.value:
        closed.append("wind")
    if obs.rr1_mm is None or obs.rr1_mm >= config.window_rr1_max_mm_h.value:
        closed.append("rain_rate")
    lookahead = config.window_rain_lookahead_h.value
    ahead = [o.rr1_mm for o in s.between(h, h + lookahead * HOUR) if o.rr1_mm is not None]
    rain_next = round(sum(ahead), 6) if len(ahead) >= lookahead else None
    if rain_next is None or rain_next >= config.window_rain_next_max_mm.value:
        closed.append("rain_next")
    if sulphur and (obs.t_c is None or obs.t_c >= config.window_sulphur_tmax_c.value):
        closed.append("sulphur_temp")
    return closed, rain_next


def spray_window(
    series: Sequence[HourlyObs], config: AdvisorConfig, now: datetime, *, sulphur: bool = False
) -> WindowScan:
    s = _Series(series)
    start = floor_hour(now)
    horizon = start + config.window_horizon_h.value * HOUR
    used_future = s.any_in(now, start + forecast_span(config))

    min_run = config.window_min_open_hours.value
    closed_now, rain_next_now = _hour(s, start, config, sulphur)
    next_window: tuple[datetime, datetime] | None = None
    run_start: datetime | None = None
    h = start
    while h < horizon:
        is_open = not _hour(s, h, config, sulphur)[0]
        if is_open and run_start is None:
            run_start = h
        if not is_open and run_start is not None:
            if h - run_start >= min_run * HOUR:
                next_window = (run_start, h)
                break
            run_start = None
        h += HOUR
    if next_window is None and run_start is not None and h - run_start >= min_run * HOUR:
        next_window = (run_start, h)

    open_now = next_window is not None and next_window[0] == start
    now_obs = s.by_ts.get(start)
    reason_inputs = {
        "now": now,
        "hour": start,
        "ff_kmh": now_obs.ff_kmh if now_obs else None,
        "rr1_mm": now_obs.rr1_mm if now_obs else None,
        "t_c": now_obs.t_c if now_obs else None,
        "rain_next_mm": rain_next_now,
        "sulphur": sulphur,
        "closed_by": closed_now,
        "next_window": next_window,
    }
    note = REPLAY_NOTE if used_future else "no forecast hours after now"
    reason = config.explain(RULE, THRESHOLDS, reason_inputs, "open" if open_now else "closed", note)
    return WindowScan(open_now, next_window, used_future, reason)


def closed_by(
    series: Sequence[HourlyObs],
    hours: Sequence[datetime],
    config: AdvisorConfig,
    *,
    sulphur: bool = False,
) -> dict[datetime, list[str]]:
    """The conditions that close each of `hours` under the scan's own hourly test (empty: the
    hour is open). For a caller that grades hours on its own series (the forecast app)."""
    s = _Series(series)
    return {h: _hour(s, h, config, sulphur)[0] for h in hours}
