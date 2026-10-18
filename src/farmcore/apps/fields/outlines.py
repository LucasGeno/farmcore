"""Decorative parcel identities: farm-scoped, north-up SVGs with no map dependency."""

from __future__ import annotations

import json
import math
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from farmcore.core.access import Repo
from farmcore.core.models.canonical import Parcel
from farmcore.web import is_sunlight

router = APIRouter()
MAX_ID = 2**63 - 1  # PostgreSQL bigint, the `parcel.id` type
ParcelId = Annotated[int, Path(ge=1, le=MAX_ID)]
MAX_AGE_S = 300  # silhouettes are decoration: a list re-renders without refetching them

MISSING = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" data-boundary="missing">'
    '<text x="50" y="57" text-anchor="middle" fill="rgb(87 94 87)" font-size="24">—</text></svg>'
)

# SVG documents cannot inherit the embedding page's CSS. Mirror the brand-50 / brand-700
# role tokens from core/static/farmcore.css, including its Sunlight override.
SVG_PALETTE = {False: ("230 239 232", "46 109 79"), True: ("232 245 236", "20 83 45")}


def outline_svg(geometry: dict, *, sunlight: bool = False) -> str:
    polygons = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        polygons = [polygons]
    rings = [ring for polygon in polygons for ring in polygon if ring]
    projected = [
        [
            (
                float(lon),
                -math.degrees(math.asinh(math.tan(math.radians(max(-85, min(85, lat)))))),
            )
            for lon, lat, *_ in ring
        ]
        for ring in rings
    ]
    points = [point for ring in projected for point in ring]
    if not points:
        return MISSING
    x0 = min(p[0] for p in points)
    y0 = min(p[1] for p in points)
    width = max(p[0] for p in points) - x0
    height = max(p[1] for p in points) - y0
    if max(width, height) == 0:
        return MISSING
    scale = 80 / max(width, height)
    left, top = (100 - width * scale) / 2, (100 - height * scale) / 2
    parts = []
    for ring in projected:
        coords = [
            f"{round((x - x0) * scale + left, 2):g},{round((y - y0) * scale + top, 2):g}"
            for x, y in ring
        ]
        parts.append("M" + " L".join(coords) + " Z")
    fill, stroke = SVG_PALETTE[sunlight]
    path = " ".join(parts)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<path d="{path}" fill="rgb({fill})" stroke="rgb({stroke})" '
        'stroke-width="2" stroke-linejoin="round" fill-rule="evenodd"/></svg>'
    )


@router.get("/parcels/{parcel_id}/outline.svg")
def parcel_outline(request: Request, repo: Repo, parcel_id: ParcelId) -> Response:
    """The parcel's silhouette, farm-scoped. Decorative: the read writes no access_log row (the
    map already logs this viewer's geometry read) and is cached for `MAX_AGE_S` with an ETag on
    farm, parcel and palette; a matching `If-None-Match` gets 304 only after the farm check."""
    statement = select(func.ST_AsGeoJSON(Parcel.geom)).where(
        Parcel.id == parcel_id, Parcel.farm_id == repo.farm_id
    )
    geometry = repo.execute_unaudited(
        statement, scope="fields:read", resource="parcel"
    ).scalar_one_or_none()
    if geometry is None:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    sunlight = is_sunlight(request)
    etag = f'W/"outline-{repo.farm_id}-{parcel_id}-{"sunlight" if sunlight else "normal"}"'
    headers = {"Cache-Control": f"private, max-age={MAX_AGE_S}", "ETag": etag, "Vary": "Cookie"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(
        outline_svg(json.loads(geometry), sunlight=sunlight),
        media_type="image/svg+xml",
        headers=headers,
    )
