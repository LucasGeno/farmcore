"""Downy mildew primary infection: the 3-10 rule on rolling hourly windows (never calendar days).

At each hour H the window is the `downy_window_h` rows with ts in (H - window, H]. An event is the
hour where R48 ≥ rain_min and T48 (mean) ≥ temp_min first becomes true (edge-triggered); the rule
re-arms only after R48 drops below rain_min. Missing rain rows are skipped, not zeroed, and an
incomplete window in the active lookback raises `data_gap`, including the hours between the last
row and the current hour (`now` floored to the hour) when the series stops early (ingest lag).
Reads only rows with ts ≤ now.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.types import HourlyObs, ceil_hour, floor_hour

RULE = "downy_3_10"
THRESHOLDS = ("downy_window_h", "downy_rain_min_mm", "downy_temp_min_c", "downy_event_max_days")


@dataclass(frozen=True, slots=True)
class DownyScan:
    events: list[datetime]  # every event hour from `start` to now
    r48_mm: float | None  # at the latest row ≤ now
    t48_c: float | None
    data_gap_hours: list[datetime]  # window end hours in the lookback with an incomplete window


def scan(
    past: Sequence[HourlyObs], config: AdvisorConfig, start: datetime, now: datetime
) -> DownyScan:
    """Edge-triggered 3-10 events at window-end hours in [start, now]; `past`: ts ≤ now, sorted."""
    window = timedelta(hours=config.downy_window_h.value)
    expected_rows = config.downy_window_h.value
    rain_min = config.downy_rain_min_mm.value
    temp_min = config.downy_temp_min_c.value
    lookback_start = now - timedelta(days=config.downy_event_max_days.value)

    rows: deque[HourlyObs] = deque()
    rain_sum = 0.0
    rain_n = 0
    temp_sum = 0.0
    temp_n = 0
    armed = True
    events: list[datetime] = []
    gaps: list[datetime] = []
    r48: float | None = None
    t48: float | None = None

    for obs in past:
        if obs.ts > now:  # no future leak, even if a caller passes the whole series
            break
        rows.append(obs)
        if obs.rr1_mm is not None:
            rain_sum += obs.rr1_mm
            rain_n += 1
        if obs.t_c is not None:
            temp_sum += obs.t_c
            temp_n += 1
        while rows[0].ts <= obs.ts - window:
            old = rows.popleft()
            if old.rr1_mm is not None:
                rain_sum -= old.rr1_mm
                rain_n -= 1
            if old.t_c is not None:
                temp_sum -= old.t_c
                temp_n -= 1
        if obs.ts < start:
            continue
        r48 = round(rain_sum, 6)
        t48 = temp_sum / temp_n if temp_n else None
        if obs.ts >= lookback_start and rain_n < expected_rows:
            gaps.append(obs.ts)
        condition = r48 >= rain_min and t48 is not None and t48 >= temp_min
        if condition and armed:
            events.append(obs.ts)
            armed = False
        elif r48 < rain_min:
            armed = True

    # Hours with no row at the end of the series: their windows are incomplete too.
    hour = timedelta(hours=1)
    tail = ceil_hour(max(rows[-1].ts + hour if rows else start, start, lookback_start))
    while tail <= floor_hour(now):
        gaps.append(tail)
        tail += hour

    return DownyScan(events=events, r48_mm=r48, t48_c=t48, data_gap_hours=gaps)
