"""The farm's location facts: coordinates, time zone and country, derived without a network.

Time zone comes from `timezonefinder` (MIT code) over its ODbL boundary data (installed as
`timezonefinder-data`, see `THIRD_PARTY_NOTICES.md`). Country comes from the bundled Natural
Earth 1:110m Admin 0 Countries file (public domain; see `core/geo/README.md`). Sea and border
points are not guessed: `locate` returns a question for the owner instead. The farm's time
zone lives in `farm.props["time_zone"]`; every farm-scoped display falls back to the installation
`display_tz` until an owner sets one.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from farmcore.core.models.canonical import Farm
from farmcore.settings import Settings

LOCATION_KEY = "forecast_location"
TIME_ZONE_KEY = "time_zone"
COUNTRY_KEY = "country"
COUNTRY_NAME_KEY = "country_name"
# A point this close to a country border is ambiguous at the 1:110m coastline resolution.
BORDER_TOLERANCE_DEGREES = 0.002

GeoPoint = tuple[float, float]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Located:
    """What the offline lookup knows about a point. `question` is set when the owner must
    choose: `"sea"` (no country), `"border"` (more than one country, or on a boundary) and
    `"time zone"` (a country but no time zone)."""

    latitude: float
    longitude: float
    time_zone: str | None = None
    country: str | None = None
    country_name: str | None = None
    question: str | None = None


@dataclass(frozen=True)
class _Polygon:
    rings: tuple[tuple[GeoPoint, ...], ...]  # exterior first, then holes
    bbox: BBox


@dataclass(frozen=True)
class _Country:
    iso: str
    name: str
    polygons: tuple[_Polygon, ...]


def _flag(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def valid_coordinate(latitude: Any, longitude: Any) -> bool:
    """A finite latitude in [-90, 90] and longitude in [-180, 180] (booleans are not values)."""
    return (
        _flag(latitude)
        and _flag(longitude)
        and -90 <= float(latitude) <= 90
        and -180 <= float(longitude) <= 180
    )


def valid_time_zone(value: Any) -> bool:
    """A key the bundled tz database knows."""
    if not isinstance(value, str) or not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _bbox(points: Iterable[GeoPoint]) -> BBox:
    xs, ys = zip(*points, strict=True)
    return min(xs), min(ys), max(xs), max(ys)


def _ring(raw: list[list[float]]) -> tuple[GeoPoint, ...]:
    return tuple((float(x), float(y)) for x, y in raw)


def _polygons(geometry: dict[str, Any]) -> tuple[_Polygon, ...]:
    raw = geometry["coordinates"]
    polygons = [raw] if geometry["type"] == "Polygon" else raw
    out = []
    for rings in polygons:
        parsed = tuple(_ring(ring) for ring in rings)
        out.append(_Polygon(parsed, _bbox(parsed[0])))
    return tuple(out)


@lru_cache(maxsize=1)
def _countries() -> tuple[_Country, ...]:
    path = files("farmcore.core").joinpath("geo/countries.geojson")
    document = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        _Country(
            iso=feature["properties"]["iso"],
            name=feature["properties"]["name"],
            polygons=_polygons(feature["geometry"]),
        )
        for feature in document["features"]
    )


@lru_cache(maxsize=1)
def _by_iso() -> dict[str, _Country]:
    return {country.iso: country for country in _countries()}


def country_options() -> list[tuple[str, str]]:
    """`(ISO alpha-2, English name)` for every bundled country, sorted by name."""
    return sorted(((c.iso, c.name) for c in _countries()), key=lambda item: item[1])


def country_name(iso: Any) -> str | None:
    """The English name of `iso` (a bundled country), else None."""
    country = _by_iso().get(iso) if isinstance(iso, str) else None
    return country.name if country else None


def _contains(bbox: BBox, x: float, y: float) -> bool:
    min_x, min_y, max_x, max_y = bbox
    return min_x <= x <= max_x and min_y <= y <= max_y


def _in_ring(x: float, y: float, ring: tuple[GeoPoint, ...]) -> bool:
    """Ray casting (even-odd); boundary cases are settled by `_near_boundary`."""
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def _segment_distance_sq(x: float, y: float, a: GeoPoint, b: GeoPoint) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length))
    return (x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2


def _on_boundary(polygon: _Polygon, x: float, y: float) -> bool:
    limit = BORDER_TOLERANCE_DEGREES**2
    return any(
        _segment_distance_sq(x, y, ring[i], ring[i + 1]) <= limit
        for ring in polygon.rings
        for i in range(len(ring) - 1)
    )


def _inside(polygon: _Polygon, x: float, y: float) -> bool:
    exterior, *holes = polygon.rings
    return _in_ring(x, y, exterior) and not any(_in_ring(x, y, hole) for hole in holes)


@lru_cache(maxsize=1)
def _finder():
    # One instance per process: loading the bundled boundary data is the expensive part.
    from timezonefinder import TimezoneFinder

    return TimezoneFinder()


def locate(latitude: Any, longitude: Any) -> Located:
    """Time zone and country for a point, offline. A sea or border point asks the owner."""
    if not valid_coordinate(latitude, longitude):
        raise ValueError("latitude and longitude must be numbers on Earth")
    lat, lon = float(latitude), float(longitude)
    hits: dict[str, _Country] = {}
    border = False
    for country in _countries():
        for polygon in country.polygons:
            if not _contains(polygon.bbox, lon, lat):
                continue
            border = border or _on_boundary(polygon, lon, lat)
            if _inside(polygon, lon, lat):
                hits[country.iso] = country
    time_zone = _finder().timezone_at(lat=lat, lng=lon)
    if not hits:
        return Located(lat, lon, question="sea")
    only = next(iter(hits.values())) if len(hits) == 1 else None
    if len(hits) > 1 or border:
        return Located(
            lat,
            lon,
            time_zone=time_zone,
            country=only.iso if only else None,
            country_name=only.name if only else None,
            question="border",
        )
    if time_zone is None:
        return Located(lat, lon, country=only.iso, country_name=only.name, question="time zone")
    return Located(lat, lon, time_zone, only.iso, only.name)


def _props(farm: Farm | None) -> dict[str, Any]:
    return farm.props if farm is not None and isinstance(farm.props, dict) else {}


def farm_time_zone(farm: Farm | None, settings: Settings) -> str:
    """The farm's own IANA time zone, else the installation `display_tz`."""
    zone = _props(farm).get(TIME_ZONE_KEY)
    return zone if valid_time_zone(zone) else settings.display_tz


def time_zone_of(session: Session, farm_id: int, settings: Settings) -> str:
    """`farm_time_zone` for the farm `farm_id` (a plain read, no access_log row)."""
    return farm_time_zone(session.get(Farm, farm_id), settings)


def farm_zone(farm: Farm | None, settings: Settings) -> ZoneInfo:
    return ZoneInfo(farm_time_zone(farm, settings))


def farm_country(farm: Farm | None) -> str | None:
    value = _props(farm).get(COUNTRY_KEY)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{2}", value) else None


def farm_location(farm: Farm | None) -> dict[str, float] | None:
    """`{"latitude": ..., "longitude": ...}` when the farm has a stored location."""
    value = _props(farm).get(LOCATION_KEY)
    if not isinstance(value, dict):
        return None
    if not valid_coordinate(value.get("latitude"), value.get("longitude")):
        return None
    return {"latitude": float(value["latitude"]), "longitude": float(value["longitude"])}


def location_needed(farm: Farm | None) -> bool:
    """First run: the farm has no usable location yet."""
    return farm_location(farm) is None
