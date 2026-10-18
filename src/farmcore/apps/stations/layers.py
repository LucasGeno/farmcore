"""`GET /stations/layer.geojson`: the "Weather" map layer (`MapLayer("weather", ...)`).

The farm's own station only (`service.station_for_farm`: `Asset.kind == "station"`, never the
imported `regional` rows) as one Point feature:

- `t_c`: the temperature of the latest reading at or before `clock.now()`;
- `rain_48h_mm`: the rain of the hours in (now - 48 h, now] (`rr1_mm` at T is the rain of the
  hour ending at T), with `rain_48h_hours` known of 48;
- `as_of`: local HH:MM of that latest reading; `as_of_text`: the same with its day, in the
  Today weather card's words (`today 06:00`, `Sun 06:00`), so an old reading never reads as
  today's; `stale`: older than 2 h;
- `url`: the Weather page.

A missing value is null, never 0. No station, or no reading up to now: an empty collection.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from farmcore.apps.stations import service
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import Asset, Observation
from farmcore.web import context_url

router = APIRouter()

RAIN_WINDOW = timedelta(hours=48)
STALE_AFTER = timedelta(hours=2)
PAGE_URL = "/stations/"


def _geojson(features: list[dict]) -> JSONResponse:
    return JSONResponse(
        {"type": "FeatureCollection", "features": features}, media_type="application/geo+json"
    )


@router.get("/layer.geojson")
def weather_layer(request: Request, repo: Repo) -> JSONResponse:
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    station = service.station_for_farm(repo)
    if station is None:
        return _geojson([])
    latest = repo.list(
        Observation,
        Observation.asset_id == station.id,
        Observation.ts <= now,
        scope=service.SCOPE,
        order_by=Observation.ts.desc(),
        limit=1,
    )
    if not latest:
        return _geojson([])
    at = latest[0].ts
    zone = ZoneInfo(settings.display_tz)
    row = service.load_hourly(repo, station.id, at, at)[0]
    window = [
        r
        for r in service.load_hourly(repo, station.id, now - RAIN_WINDOW, now)
        if r["ts"] > now - RAIN_WINDOW
    ]
    rain = [r["rr1_mm"] for r in window if r["rr1_mm"] is not None]
    point = repo.execute(
        select(func.ST_X(Asset.geom), func.ST_Y(Asset.geom)).where(
            Asset.id == station.id, Asset.farm_id == repo.farm_id, Asset.kind == "station"
        ),
        scope=service.SCOPE,
        resource="asset",
    ).one()
    if point[0] is None:
        return _geojson([])
    return _geojson(
        [
            {
                "type": "Feature",
                "id": station.id,
                "geometry": {"type": "Point", "coordinates": [point[0], point[1]]},
                "properties": {
                    "id": station.id,
                    "name": station.name,
                    "t_c": row["t_c"],
                    "rain_48h_mm": round(sum(rain), 1) if rain else None,
                    "rain_48h_hours": len(rain),
                    "as_of": at.astimezone(zone).strftime("%H:%M"),
                    "as_of_text": service.reading_when(at, now, zone),
                    "stale": at < now - STALE_AFTER,
                    "url": context_url(request, PAGE_URL),
                },
            }
        ]
    )
