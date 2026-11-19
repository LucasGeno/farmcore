"""Hourly weather as the apps read it: `HourlyObs` rows on whole UTC hours.

Shared by every app that reads hourly weather (forecast receipts, crop protection, planning,
KPIs), so none of them needs another app to read the weather. All timestamps are
timezone-aware: `HourlyObs` raises `ValueError` on a naive `ts` and holds it in UTC.
`HourlyObs.rr1_mm` is the rain in the hour ending at `ts` (Météo-France RR1); `None` means the
value is missing, never zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def aware_utc(ts: datetime, what: str) -> datetime:
    """`ts` in UTC; a naive `ts` is a `ValueError` naming `what`."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got naive {ts.isoformat()}")
    return ts.astimezone(UTC)


def floor_hour(ts: datetime) -> datetime:
    """`ts` floored to the hour (region UTC offsets are whole hours)."""
    return ts.replace(minute=0, second=0, microsecond=0)


def ceil_hour(ts: datetime) -> datetime:
    floor = floor_hour(ts)
    return floor if floor == ts else floor + timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class HourlyObs:
    ts: datetime
    rr1_mm: float | None = None
    t_c: float | None = None
    rh_pct: float | None = None
    ff_kmh: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", aware_utc(self.ts, "HourlyObs.ts"))
