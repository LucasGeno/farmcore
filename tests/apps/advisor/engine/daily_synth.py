"""Synthetic daily series builder for daily-engine tests (not a test module).

Every value here is invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from farmcore.apps.advisor.engine.daily import DailyObs

DAY = timedelta(days=1)


def days(
    start: date,
    end: date,
    *,
    precip: float | None = 0.0,
    tmean: float | None = 15.0,
    tmin: float | None = 10.0,
    tmax: float | None = 20.0,
    rh: float | None = 70.0,
    wind: float | None = 2.0,
    overrides: Mapping[date, Mapping[str, float | None]] | None = None,
) -> list[DailyObs]:
    """Every local date in [start, end] with calm, dry, mild defaults; `overrides[d]` replaces
    fields (keys as in `DailyObs`)."""
    overrides = overrides or {}
    out: list[DailyObs] = []
    d = start
    while d <= end:
        fields: dict[str, float | None] = {
            "precip_mm": precip,
            "tmean_c": tmean,
            "tmin_c": tmin,
            "tmax_c": tmax,
            "rh_pct": rh,
            "wind_ms": wind,
        }
        fields.update(overrides.get(d, {}))
        out.append(DailyObs(date=d, **fields))
        d += DAY
    return out
