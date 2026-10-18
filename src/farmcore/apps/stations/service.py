"""Stations service (contract for advisor 13 and baseline 15). Reads go through the
`ScopedRepository` with scope `stations:read`, never a raw session."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from farmcore.apps.stations.connector import VARIABLES
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Asset, Observation

SCOPE = "stations:read"


def station_for_farm(repo: ScopedRepository) -> Asset | None:
    """The farm's weather station (the first by id), or None."""
    rows = repo.list(Asset, Asset.kind == "station", scope=SCOPE, order_by=Asset.id, limit=1)
    return rows[0] if rows else None


def load_hourly(
    repo: ScopedRepository, asset_id: int, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Hourly rows with `start <= ts <= end`, oldest first:
    `{ts, rr1_mm, t_c, rh_pct, ff_kmh}`, a variable missing that hour is None (never 0).
    Only hours with at least one stored value appear. Callers bound `end` (replay: clock now
    for observed values, now + 72 h for the replay forecast)."""
    obs = repo.list(
        Observation,
        Observation.asset_id == asset_id,
        Observation.ts >= start,
        Observation.ts <= end,
        scope=SCOPE,
        order_by=(Observation.ts, Observation.variable),
    )
    by_ts: dict[datetime, dict[str, Any]] = {}
    for o in obs:
        row = by_ts.setdefault(o.ts, {"ts": o.ts, **dict.fromkeys(VARIABLES)})
        if o.variable in VARIABLES:
            row[o.variable] = o.value
    return list(by_ts.values())


def reading_when(ts: datetime, now: datetime, zone: ZoneInfo) -> str:
    """A reading time in words: `today 14:00`, `tomorrow 07:00` or `Fri 07:00` (local)."""
    local, days = (
        ts.astimezone(zone),
        (ts.astimezone(zone).date() - now.astimezone(zone).date()).days,
    )
    day = {0: "today", 1: "tomorrow"}.get(days, local.strftime("%a"))
    return f"{day} {local:%H:%M}"
