"""Leaf wetness per hour: measured by a sensor, or estimated from relative humidity.

A sensor's wet minutes in the hour are used as they are (`origin="measured"`). Without one, an
hour whose relative humidity is at or above a threshold counts as 60 wet minutes, otherwise 0
(`origin="estimated"`), and the hour carries the threshold `Parameter` it was judged by
(`basis`). An hour with neither is `missing`, never dry. A duration over hours is
`measured` only when every hour was measured; any estimated or missing hour makes it
`estimated`, with the thresholds used and the count of missing hours.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from farmcore.core.model_spec import Parameter
from farmcore.core.weather.hourly import aware_utc

RH_WET_THRESHOLD = Parameter(
    name="rh_wet_threshold",
    value=90.0,
    unit="%",
    source=(
        "assumption: an hour with relative humidity at or above 90 % is wet, the leaf-wetness "
        "proxy the existing crop rules use; awaiting agronomist review"
    ),
    status="assumed",
)


@dataclass(frozen=True, slots=True)
class HourWetness:
    ts: datetime  # UTC, the hour as the series gives it
    wet_minutes: float | None  # 0-60; None when missing
    origin: str  # measured | estimated | missing
    basis: Parameter | None  # the threshold an estimate used; None otherwise


class WetDuration(NamedTuple):
    hours: float  # wet hours over the known hours
    origin: str  # measured | estimated
    bases: tuple[Parameter, ...]  # the thresholds the estimated hours used
    missing: int  # hours with neither a sensor value nor humidity


def leaf_wetness(
    hours: Iterable[tuple[datetime, float | None]],
    *,
    measured: Mapping[datetime, float] | None = None,
    threshold: Parameter = RH_WET_THRESHOLD,
) -> list[HourWetness]:
    """Wetness of each `(ts, relative humidity %)` hour; `measured` maps an hour to a sensor's
    wet minutes (0-60) and wins over humidity. A measured hour not in `hours` is an error,
    so a timezone slip cannot silently fall back to estimates."""
    if threshold.status not in ("sourced", "assumed") or threshold.unit != "%":
        raise ValueError("threshold must be a sourced or assumed Parameter in %")
    sensor: dict[datetime, float] = {}
    for ts, minutes in (measured or {}).items():
        if not 0 <= minutes <= 60:
            raise ValueError(f"measured wet minutes must be 0-60, got {minutes!r} at {ts}")
        sensor[aware_utc(ts, "measured hour")] = minutes

    out: list[HourWetness] = []
    for ts, rh in hours:
        ts = aware_utc(ts, "hour")
        if rh is not None and not 0 <= rh <= 100:
            raise ValueError(f"relative humidity must be 0-100 %, got {rh!r} at {ts}")
        if ts in sensor:
            out.append(HourWetness(ts, sensor.pop(ts), "measured", None))
        elif rh is None:
            out.append(HourWetness(ts, None, "missing", None))
        else:
            wet = 60.0 if rh >= threshold.value else 0.0
            out.append(HourWetness(ts, wet, "estimated", threshold))
    if sensor:
        raise ValueError(f"measured hours not in the series: {sorted(sensor)[:3]}")
    return out


def wet_duration(hours: Sequence[HourWetness]) -> WetDuration:
    """Total wet hours, labelled `measured` only when every hour was measured."""
    total = sum(h.wet_minutes for h in hours if h.wet_minutes is not None) / 60
    bases = tuple(dict.fromkeys(h.basis for h in hours if h.basis is not None))
    missing = sum(h.origin == "missing" for h in hours)
    measured = bool(hours) and all(h.origin == "measured" for h in hours)
    return WetDuration(total, "measured" if measured else "estimated", bases, missing)
