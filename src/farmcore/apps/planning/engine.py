"""Pure resource-aware scheduling: which hours can this job actually run in?

The weather test is the advisor's own (`advisor.engine.window.closed_by`); no new agronomic
threshold is introduced here. On top of it this module combines the job's estimated duration,
the configured availability windows of its machine and operator, their existing bookings and the
machine's known work rate. Every state is explicit: an input that is not configured is
``unknown``, never silently treated as safe or free.

The module is pure (no database): `offers` reads the farm state and passes plain windows/spans,
so the rules are testable with worked examples and cannot mutate anything.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs
from farmcore.apps.advisor.engine.types import ceil_hour, floor_hour
from farmcore.apps.advisor.engine.window import closed_by

HOUR = timedelta(hours=1)
DEFAULT_HORIZON_HOURS = 72


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open ``[start, end)`` period."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("a span needs timezone-aware times")
        if self.start >= self.end:
            raise ValueError("a span must end after it starts")

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class Window:
    """One configured availability period; ``available=False`` overrides a positive overlap."""

    start: datetime
    end: datetime
    available: bool

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("an availability window must end after it starts")


@dataclass(frozen=True, slots=True)
class State:
    status: str  # available | busy | open | closed | unknown
    reason: str


@dataclass(frozen=True, slots=True)
class CapacityState:
    status: str  # fits | too_short | unknown
    reason: str
    required_minutes: float | None = None


@dataclass(frozen=True, slots=True)
class Assessment:
    status: str  # feasible | infeasible | unknown
    reasons: tuple[str, ...]
    weather: State
    machine: State | None
    operator: State | None
    capacity: CapacityState | None


def _overlaps(window: Window, span: Span) -> bool:
    return window.start < span.end and span.start < window.end


def _covered(intervals: Sequence[tuple[datetime, datetime]], span: Span) -> bool:
    """Whether the union of half-open ``intervals`` covers ``span`` continuously."""
    current = span.start
    for start, end in sorted(intervals):
        if start > current:
            return False
        if end > current:
            current = end
        if current >= span.end:
            return True
    return current >= span.end


def _clock(value: datetime, tz: str) -> str:
    return value.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def availability(windows: Sequence[Window], span: Span, *, tz: str = "Europe/Paris") -> State:
    """State of one resource over ``span`` from its configured windows.

    A configured busy window that intersects is ``busy``. Positive windows must cover the whole
    span; uncovered time is also ``busy`` (outside configured availability), not silently free.
    No windows at all means ``unknown``: the farmer has not configured this resource.
    """
    busy = [w for w in windows if not w.available and _overlaps(w, span)]
    if busy:
        first = min(busy, key=lambda w: w.start)
        return State(
            "busy",
            f"marked unavailable {_clock(first.start, tz)}–{_clock(first.end, tz)}.",
        )
    positive = [(w.start, w.end) for w in windows if w.available]
    if _covered(positive, span):
        return State("available", "configured available for this window.")
    if positive:
        return State(
            "busy",
            f"outside configured availability for {_clock(span.start, tz)}–{_clock(span.end, tz)}.",
        )
    return State("unknown", "no configured availability for this resource; unknown, not free.")


def capacity_state(
    work_rate_ha_per_hour: Decimal | float | None,
    *,
    area_ha: Decimal | float,
    duration_minutes: int,
) -> CapacityState:
    """Whether the machine's known work rate fits the job duration.

    ``None`` (or an unusable value) is ``unknown``: no fit can be claimed.
    """
    if work_rate_ha_per_hour is None:
        return CapacityState(
            "unknown",
            "machine capacity (work rate) is not configured; no fit can be claimed.",
        )
    rate = float(work_rate_ha_per_hour)
    area = float(area_ha)
    if rate <= 0 or area < 0:
        return CapacityState("unknown", "machine capacity is not usable; no fit can be claimed.")
    required = area / rate * 60.0
    if required > duration_minutes:
        return CapacityState(
            "too_short",
            f"capacity needs about {required:.0f} minutes; the job allows {duration_minutes}.",
            required,
        )
    return CapacityState(
        "fits",
        f"capacity needs about {required:.0f} minutes of the {duration_minutes} planned.",
        required,
    )


def _words(causes: set[str]) -> str:
    parts: list[str] = []
    if causes & {"rain_rate", "rain_next"}:
        parts.append("rain")
    if "wind" in causes:
        parts.append("wind")
    if "sulphur_temp" in causes:
        parts.append("too hot for sulphur")
    return ", ".join(parts)


def weather_state(
    series: Sequence[HourlyObs],
    span: Span,
    config: AdvisorConfig,
    *,
    sulphur: bool = False,
) -> State:
    """Open/closed/unknown for every whole hour the job overlaps."""
    hours: list[datetime] = []
    hour = floor_hour(span.start)
    while hour < span.end:
        hours.append(hour)
        hour += HOUR
    if not hours:  # pragma: no cover - Span requires start < end, so floor(start) is a candidate
        return State("unknown", "the job window contains no forecast hour.")
    closed = closed_by(series, hours, config, sulphur=sulphur)
    if any("no_data" in closed[hour] for hour in hours):
        return State(
            "unknown",
            "the forecast does not cover this window; spraying cannot be claimed as suitable.",
        )
    if any(closed[hour] for hour in hours):
        causes = {cause for hour in hours for cause in closed[hour]}
        return State("closed", f"weather closes the window: {_words(causes) or 'conditions'}.")
    return State("open", "forecast hours are open for spraying.")


def assess(
    weather: State,
    machine: State | None,
    operator: State | None,
    capacity: CapacityState | None,
) -> Assessment:
    """Combine the independent states. Only all-positive is feasible."""
    reasons: list[str] = []
    negative = False
    unknown = False
    for label, state in (("Weather", weather), ("Machine", machine), ("Operator", operator)):
        if state is None:
            continue
        if state.status in {"closed", "busy"}:
            negative = True
            reasons.append(f"{label}: {state.reason}")
        elif state.status == "unknown":
            unknown = True
            reasons.append(f"{label}: {state.reason}")
    if capacity is not None:
        if capacity.status == "too_short":
            negative = True
            reasons.append(f"Capacity: {capacity.reason}")
        elif capacity.status == "unknown":
            unknown = True
            reasons.append(f"Capacity: {capacity.reason}")
    if negative:
        return Assessment("infeasible", tuple(reasons), weather, machine, operator, capacity)
    if unknown:
        return Assessment("unknown", tuple(reasons), weather, machine, operator, capacity)
    return Assessment("feasible", (), weather, machine, operator, capacity)


def _band_ok(span: Span, config: AdvisorConfig, tz: str) -> bool:
    """Every hour of the job lies in the advisor's local daytime band."""
    zone = ZoneInfo(tz)
    start_hour = config.calendar.window_day_start_hour_local.value
    end_hour = config.calendar.window_day_end_hour_local.value
    hour = floor_hour(span.start)
    while hour < span.end:
        local = hour.astimezone(zone)
        if not start_hour <= local.hour < end_hour:
            return False
        hour += HOUR
    return True


def next_feasible_span(
    *,
    series: Sequence[HourlyObs],
    now: datetime,
    tz: str,
    config: AdvisorConfig,
    earliest: datetime,
    duration_minutes: int,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    sulphur: bool = False,
    operator_windows: Sequence[Window] | None = None,
    machine_windows: Sequence[Window] | None = None,
    operator_bookings: Sequence[Span] | None = None,
    machine_bookings: Sequence[Span] | None = None,
    work_rate: Decimal | float | None = None,
    area_ha: Decimal | float = Decimal("0"),
    busy_days: Collection[date] = (),
) -> Span | None:
    """The earliest start from ``earliest`` where weather, resources, bookings and capacity agree.

    ``None`` for a resource means the job has no such assignment; ``work_rate=None`` means the
    machine's capacity is unknown, so a candidate can only be offered when no machine is
    assigned. Unknown availability is never treated as free.
    """
    duration = timedelta(minutes=duration_minutes)
    start = ceil_hour(max(earliest, now))
    horizon = max(earliest, now) + timedelta(hours=horizon_hours)
    while start < horizon:
        span = Span(start, start + duration)
        if not _band_ok(span, config, tz):
            start += HOUR
            continue
        if span.start.astimezone(ZoneInfo(tz)).date() in busy_days:
            start += HOUR
            continue
        if weather_state(series, span, config, sulphur=sulphur).status != "open":
            start += HOUR
            continue
        if (
            machine_windows is not None
            and availability(machine_windows, span).status != "available"
        ):
            start += HOUR
            continue
        if (
            operator_windows is not None
            and availability(operator_windows, span).status != "available"
        ):
            start += HOUR
            continue
        if machine_bookings is not None and any(b.overlaps(span) for b in machine_bookings):
            start += HOUR
            continue
        if operator_bookings is not None and any(b.overlaps(span) for b in operator_bookings):
            start += HOUR
            continue
        if work_rate is not None and (
            capacity_state(work_rate, area_ha=area_ha, duration_minutes=duration_minutes).status
            != "fits"
        ):
            start += HOUR
            continue
        return span
    return None
