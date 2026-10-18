"""Stations pages: `/stations/` (stations + connector health), `/stations/{id}` (hourly chart).

The chart spans clock now - 72 h .. now + 72 h on a full hourly grid (missing values are
gaps, never 0); in replay the hours after now are observed weather shown as the forecast.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.stations import conditions, service
from farmcore.apps.stations.connector import REPLAY_HORIZON, VARIABLES
from farmcore.core.access import Repo, ScopedRepository
from farmcore.core.clock import ReplayClock, get_clock
from farmcore.core.models.canonical import Asset, Observation
from farmcore.web import LOCAL_FORMAT, render

router = APIRouter()
HOUR = timedelta(hours=1)
CHART_PAST = timedelta(hours=72)


def _span(delta: timedelta) -> str:
    """`3 days` (a day or more, in whole days, halves up: 36 h reads `2 days`, 60 h `3 days`,
    73 h `3 days`), else `5 hours`."""
    hours = int(delta / HOUR)
    if hours >= 24:
        days = (hours + 12) // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _newest_at_or_before(repo: ScopedRepository, station: Asset, now: datetime) -> datetime | None:
    """The newest stored reading hour of `station` at or before `now`, or None."""
    rows = repo.list(
        Observation,
        Observation.asset_id == station.id,
        Observation.ts <= now,
        scope=service.SCOPE,
        order_by=Observation.ts.desc(),
        limit=1,
    )
    return rows[0].ts if rows else None


def _health(repo: ScopedRepository, station: Asset, now: datetime) -> dict[str, Any]:
    """Connector health split at clock `now`: what was observed up to now, and the stored hours
    after now (replay: the observed future shown as the forecast). Never a "latest hour" ahead
    of the clock: with data ahead, "observed up to" is the newest stored hour at or before now
    (None when now is before the first reading)."""
    p = station.props

    def ts(key: str) -> datetime | None:
        return datetime.fromisoformat(p[key]) if p.get(key) else None

    last_ts = ts("last_ts")
    ahead = last_ts is not None and last_ts > now
    return {
        "archive": p.get("archive") if p.get("archive_only") else None,
        "archive_url": p.get("archive_page"),
        "observed_until": _newest_at_or_before(repo, station, now) if ahead else last_ts,
        "forecast_until": last_ts if ahead else None,
        "forecast_span": _span(last_ts - now) if ahead and last_ts else None,
        "last_run": ts("last_run"),
        "rows": p.get("rows"),
        "nulls_skipped": p.get("nulls_skipped"),
        "file": p.get("file"),
        "error": p.get("error"),
    }


def _chart(rows: list[dict[str, Any]], now: datetime, tz: str, replay: bool) -> dict[str, Any]:
    """A full hourly grid from now - 72 h to now + 72 h (hour-aligned), values or None."""
    start = (now - CHART_PAST).replace(minute=0, second=0, microsecond=0)
    end = now + REPLAY_HORIZON
    by_ts = {r["ts"].astimezone(UTC): r for r in rows}
    grid = []
    t = start
    while t <= end:
        grid.append(t)
        t += HOUR
    zone = ZoneInfo(tz)
    return {
        "labels": [g.astimezone(zone).strftime(LOCAL_FORMAT) for g in grid],
        "now_index": (now - start) / HOUR,
        "replay": replay,
        **{v: [by_ts.get(g, {}).get(v) for g in grid] for v in VARIABLES},
    }


@router.get("/", response_class=HTMLResponse)
def station_list(request: Request, repo: Repo) -> HTMLResponse:
    stations = repo.list(Asset, Asset.kind == "station", scope=service.SCOPE, order_by=Asset.id)
    now = get_clock(repo.session, request.app.state.settings).now()
    return render(
        request,
        "stations/list.html",
        stations=[{"station": s, "health": _health(repo, s, now)} for s in stations],
    )


@router.get("/{station_id}", response_class=HTMLResponse)
def station_page(station_id: int, request: Request, repo: Repo) -> HTMLResponse:
    station = repo.get(Asset, station_id, scope=service.SCOPE)
    if station is None or station.kind != "station":
        raise HTTPException(status_code=404, detail=f"No station with id {station_id}.")
    settings = request.app.state.settings
    clock = get_clock(repo.session, settings)
    now = clock.now()
    if station.props.get("archive_only"):
        return render(
            request,
            "stations/archive.html",
            station=station,
            archive=station.props["archive"],
            archive_url=station.props.get("archive_page"),
        )
    rows = service.load_hourly(repo, station.id, now - CHART_PAST - HOUR, now + REPLAY_HORIZON)
    replay = isinstance(clock, ReplayClock)
    return render(
        request,
        "stations/detail.html",
        station=station,
        health=_health(repo, station, now),
        now=now,
        replay=replay,
        chart=_chart(rows, now, settings.display_tz, replay),
        spray=conditions.spray_conditions(rows, now, settings.display_tz),
        spray_rules=conditions.rules(),
    )
