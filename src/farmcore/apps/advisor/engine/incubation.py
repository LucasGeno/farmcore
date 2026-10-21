"""Downy incubation after each 3-10 event: the Goidanich table, accumulated hour by hour.

The rate (%/day) at temperature T reads the table (config `incub_table`) by linear interpolation
between rows; the humid column applies when the hour's RH ≥ `incub_humid_rh_hourly` (an hourly
wetness proxy for the table's daily RH ≥ 75 % split). At or below `incub_tmin` the rate is 0;
between `incub_tmin` and the first row (12.0 °C) it ramps linearly from 0 to that row; above the
last row it stays at the last row.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.gate import gate_start
from farmcore.apps.advisor.engine.types import HourlyObs, RuleResult

if TYPE_CHECKING:
    from farmcore.apps.advisor.engine.decision import EvalContext

RULE = "downy_incubation"
THRESHOLDS = ("incub_tmin", "incub_humid_rh_hourly", "incub_alert_pct", "incub_symptoms_pct")
NOTE = "risk indicator, not a prediction: the Goidanich table over-predicts on V. vinifera"
HOUR = timedelta(hours=1)
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Incubation:
    event: datetime
    pct: float
    alert_at: datetime | None
    symptoms_at: datetime | None
    expected_symptoms: datetime | None  # actual crossing, else projected at the mean rate so far
    projected: bool
    missing_hours: int


def daily_rate(t_c: float, rh_pct: float, config: AdvisorConfig) -> float:
    """Incubation development in %/day at one hour's T and RH."""
    rows = config.incub_table.rows
    col = 2 if rh_pct >= config.incub_humid_rh_hourly.value else 1
    tmin = config.incub_tmin.value
    first_t = rows[0][0]
    if t_c <= tmin:
        return 0.0
    if t_c < first_t:
        return rows[0][col] * (t_c - tmin) / (first_t - tmin)
    for lo, hi in zip(rows, rows[1:], strict=False):
        if t_c <= hi[0]:
            frac = (t_c - lo[0]) / (hi[0] - lo[0])
            return lo[col] + frac * (hi[col] - lo[col])
    return rows[-1][col]


def progress(
    past: Sequence[HourlyObs], event: datetime, config: AdvisorConfig, now: datetime
) -> Incubation:
    """Incubation of one 3-10 event over rows with ts in (event, now], each adding rate / 24.

    `past` is sorted by ts. A row with T or RH missing adds nothing and counts as a missing hour,
    as does an hour with no row. Progress stops at `incub_symptoms_pct`.
    """
    alert = config.incub_alert_pct.value
    full = config.incub_symptoms_pct.value
    ts_list = [o.ts for o in past]
    pct = 0.0
    used = 0
    alert_at: datetime | None = None
    symptoms_at: datetime | None = None
    for obs in past[bisect_right(ts_list, event) : bisect_right(ts_list, now)]:
        if obs.t_c is None or obs.rh_pct is None:
            continue
        used += 1
        pct += daily_rate(obs.t_c, obs.rh_pct, config) / 24
        if alert_at is None and pct >= alert - _EPS:
            alert_at = obs.ts
        if pct >= full - _EPS:
            pct = full
            symptoms_at = obs.ts
            break
    end = symptoms_at or now
    missing = max(0, int((end - event) / HOUR) - used)
    pct = round(pct, 6)

    expected, projected = symptoms_at, False
    elapsed_h = (now - event) / HOUR
    if symptoms_at is None and pct > 0 and elapsed_h > 0:
        remaining_h = (full - pct) / (pct / elapsed_h)
        expected, projected = now + timedelta(hours=round(remaining_h)), True
    return Incubation(event, pct, alert_at, symptoms_at, expected, projected, missing)


def _verdict(inc: Incubation | None) -> str:
    if inc is None:
        return "no event"
    if inc.symptoms_at is not None:
        return "symptoms expected"
    if inc.alert_at is not None:
        return "alert"
    return "incubating"


def explain(
    past: Sequence[HourlyObs], events: Sequence[datetime], config: AdvisorConfig, now: datetime
) -> RuleResult:
    """One reason for the card: the latest event's progress; every event's progress in inputs."""
    runs = [progress(past, e, config, now) for e in events if e <= now]
    latest = runs[-1] if runs else None
    incomplete = bool(latest and latest.missing_hours)
    start = gate_start(config) - timedelta(hours=config.downy_window_h.value)
    available = {
        o.ts for o in past if start < o.ts <= now and o.rr1_mm is not None and o.t_c is not None
    }
    history_complete = len(available) >= int((now - start) / HOUR)
    inputs = {
        "coverage": "limited" if incomplete else "complete",
        "event_history_coverage": "complete" if history_complete else "limited",
        "pct_basis": "observed-hours-only" if incomplete else "complete-hours",
        "event": latest.event if latest else None,
        "pct": latest.pct if latest else None,
        "alert_at": latest.alert_at if latest and not incomplete else None,
        "symptoms_at": latest.symptoms_at if latest and not incomplete else None,
        "expected_symptoms": latest.expected_symptoms if latest and not incomplete else None,
        "projected": False if incomplete else (latest.projected if latest else None),
        "missing_hours": latest.missing_hours if latest else 0,
        "events": [
            {
                "event": r.event,
                "pct": r.pct,
                "alert_at": r.alert_at if not r.missing_hours else None,
                "symptoms_at": r.symptoms_at if not r.missing_hours else None,
                "missing_hours": r.missing_hours,
                "coverage": "limited" if r.missing_hours else "complete",
            }
            for r in runs
        ],
    }
    note = f"{NOTE}; {config.incub_table.note}"
    if not history_complete:
        note += "; incomplete event history does not establish absence of additional infections"
    reason = config.explain(
        RULE, THRESHOLDS, inputs, "not evaluable" if incomplete else _verdict(latest), note
    )
    if config.incub_table.source in reason.source:
        return reason
    return replace(reason, source=f"{reason.source}; {config.incub_table.source}")


def rule(ctx: EvalContext) -> None:
    """Extra rule: append the incubation reason (it never changes the decision)."""
    ctx.advice.reasons.append(explain(ctx.past, ctx.downy_events, ctx.config, ctx.now))
