"""Optional aerial context for one real parcel; tile requests happen only in the browser."""

from __future__ import annotations

import json
import math

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from farmcore.apps.fields.outlines import ParcelId
from farmcore.core.access import Repo
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.web import render

router = APIRouter()
TILE_URL = (
    "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
    "&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM"
    "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg"
)


def aerial_view(geometry: dict) -> dict | None:
    """A 600x400 north-up Web Mercator frame, with 40px padding and <=12 IGN tiles."""
    polygons = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        polygons = [polygons]
    rings = [
        [
            (
                (lon + 180) / 360,
                (1 - math.asinh(math.tan(math.radians(max(-85, min(85, lat))))) / math.pi) / 2,
            )
            for lon, lat, *_ in ring
        ]
        for polygon in polygons
        for ring in polygon
        if ring
    ]
    points = [point for ring in rings for point in ring]
    if not points or not all(math.isfinite(n) for point in points for n in point):
        return None
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    width = max(p[0] for p in points) - left
    height = max(p[1] for p in points) - top
    if max(width, height) == 0:
        return None
    scale = min(520 / width if width else math.inf, 320 / height if height else math.inf)
    frame_x = left - (600 / scale - width) / 2
    frame_y = top - (400 / scale - height) / 2
    zoom = max(0, min(19, math.floor(math.log2(scale / 256))))
    count = 2**zoom
    tile_size = scale / count
    tiles = [
        {
            "x": (x / count - frame_x) * scale,
            "y": (y / count - frame_y) * scale,
            "size": tile_size,
            "url": TILE_URL.format(z=zoom, x=x, y=y),
        }
        for x in range(math.floor(frame_x * count), math.ceil((frame_x + 600 / scale) * count))
        for y in range(math.floor(frame_y * count), math.ceil((frame_y + 400 / scale) * count))
    ]
    path = " ".join(
        "M"
        + " L".join(f"{(x - frame_x) * scale:.2f},{(y - frame_y) * scale:.2f}" for x, y in ring)
        + " Z"
        for ring in rings
    )
    return {"path": path, "tiles": tiles}


@router.get("/parcels/{parcel_id}/aerial", response_class=HTMLResponse)
def parcel_aerial(request: Request, repo: Repo, parcel_id: ParcelId) -> HTMLResponse:
    parcel = repo.get(Parcel, parcel_id, scope="fields:read")
    if parcel is None:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    farm = repo.get(Farm, repo.farm_id, scope="fields:read")
    synthetic = (farm.props or {}).get("layout") == "synthetic"
    view = None
    if not synthetic:
        statement = select(func.ST_AsGeoJSON(Parcel.geom)).where(
            Parcel.id == parcel.id, Parcel.farm_id == repo.farm_id
        )
        geometry = repo.execute(statement, scope="fields:read", resource="parcel").scalar_one()
        view = aerial_view(json.loads(geometry))
    template = (
        "fields/_aerial_view.html"
        if request.headers.get("HX-Request") == "true"
        else "fields/aerial.html"
    )
    response = render(request, template, parcel=parcel, aerial=view, synthetic=synthetic)
    response.headers.update({"Cache-Control": "private, no-store", "Vary": "HX-Request, Cookie"})
    return response
