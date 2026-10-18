"""Synthetic hourly series builders for engine tests (not a test module)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from farmcore.apps.advisor.engine import HourlyObs

HOUR = timedelta(hours=1)

Overrides = Mapping[datetime, Mapping[str, float | None]]


def utc(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def series(
    start: datetime,
    end: datetime,
    *,
    rr1: float | None = 0.0,
    t: float | None = 15.0,
    rh: float | None = 70.0,
    ff: float | None = 5.0,
    overrides: Overrides | None = None,
) -> list[HourlyObs]:
    """Every hour in [start, end] with calm, dry, mild defaults; `overrides[ts]` replaces fields."""
    overrides = overrides or {}
    out: list[HourlyObs] = []
    ts = start
    while ts <= end:
        fields: dict[str, float | None] = {"rr1_mm": rr1, "t_c": t, "rh_pct": rh, "ff_kmh": ff}
        fields.update(overrides.get(ts, {}))
        out.append(HourlyObs(ts=ts, **fields))
        ts += HOUR
    return out


def rain(at: datetime, mm_per_hour: float | None, hours: int) -> dict[datetime, dict]:
    """Override map: `hours` consecutive rows from `at` with `mm_per_hour` each."""
    return {at + i * HOUR: {"rr1_mm": mm_per_hour} for i in range(hours)}


def at(ts: datetime, **fields: float | None) -> dict[datetime, dict]:
    return {ts: dict(fields)}


def merge(*maps: Overrides) -> dict[datetime, dict]:
    out: dict[datetime, dict] = {}
    for m in maps:
        for ts, fields in m.items():
            out.setdefault(ts, {}).update(fields)
    return out
