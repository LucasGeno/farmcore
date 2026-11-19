"""Choose a farm's weather sources from its pin (spec R2).

`select(providers, point, country, farm_elevation)` takes the installed providers of the active
apps (`registry.installed` over `registry.live_active`) and returns:

- **stations**: every station the farm's country providers know within `SEARCH_RADIUS_M`,
  ranked by `rank`. A provider bound to other countries is never asked, nor a `live_only` one
  unless `live`; a provider that is not configured or whose catalogue fails is named in `notes`,
  and the others still answer.
- **forecast**: every forecast provider serving the farm's country, always included.
- **backfill**: every provider declaring `Capabilities.backfill` (ERA5 through Copernicus, when
  its app exists). None installed is said in `notes`, never filled from another source.

`rank` is deterministic: stations within `ELEVATION_BAND_M` of the farm (or whose elevation
cannot be compared) come first, nearest first; a station more than that much higher or lower
comes after them all, nearest first, with the reason. Ties fall to the provider name, then the
station id. A farm whose elevation is unknown says so and nothing is down-ranked: an elevation
is never invented. Distance is the great-circle distance on the sphere of the WGS84 mean radius,
in geographic coordinates, so it holds across borders and the antimeridian.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from farmcore.core.weather.contract import BBox, NotConfigured, Point, Station, WeatherProvider

# IUGG mean radius R1 of the WGS84 ellipsoid, metres (Moritz, Geodetic Reference System 1980).
EARTH_RADIUS_M = 6_371_008.8
# Assumed: a station further than this from the pin rarely describes the farm's weather.
SEARCH_RADIUS_M = 50_000
# The source-selection acceptance criterion (spec R2, ticket "Choose weather sources
# automatically from the farm pin"). Rationale: at the ICAO standard atmosphere lapse rate
# (6.5 °C/km, ICAO Doc 7488) 300 m is about 2 °C of mean temperature.
ELEVATION_BAND_M = Decimal(300)


@dataclass(frozen=True)
class Candidate:
    provider: str
    station: Station
    distance_m: float
    elevation_difference_m: Decimal | None  # station minus farm; None when either is unknown
    down_ranked: bool
    reason: str


@dataclass(frozen=True)
class Source:
    provider: str
    reason: str


@dataclass(frozen=True)
class Selection:
    stations: tuple[Candidate, ...]
    forecast: tuple[Source, ...]
    backfill: tuple[Source, ...]
    notes: tuple[str, ...]


def great_circle_m(a: Point, b: Point) -> float:
    """The haversine distance between two WGS84 points, in metres."""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def search_boxes(point: Point, radius_m: float = SEARCH_RADIUS_M) -> list[BBox]:
    """Boxes covering every point within `radius_m`: two when the circle crosses the
    antimeridian, so a station just across it is still asked for."""
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    south, north = max(-90.0, point.latitude - dlat), min(90.0, point.latitude + dlat)
    widest = max(abs(south), abs(north))
    if widest >= 89.0:
        return [BBox(-180.0, south, 180.0, north)]
    dlon = dlat / math.cos(math.radians(widest))
    west, east = point.longitude - dlon, point.longitude + dlon
    if west < -180.0:
        return [BBox(-180.0, south, east, north), BBox(west + 360.0, south, 180.0, north)]
    if east > 180.0:
        return [BBox(west, south, 180.0, north), BBox(-180.0, south, east - 360.0, north)]
    return [BBox(west, south, east, north)]


def _explain(distance_m: float, difference: Decimal | None, farm_known: bool, station: Station):
    away = f"{distance_m / 1000:.1f} km away"
    if not farm_known:
        return f"{away}; farm elevation unknown, so elevation was not compared", False
    if difference is None or station.elevation_m is None:
        return f"{away}; station elevation unknown", False
    over = abs(difference) > ELEVATION_BAND_M
    # Whole metres, rounded away from the band's edge, so 300.4 m never reads "300 m ... over".
    metres = abs(difference).quantize(Decimal(1), ROUND_UP if over else ROUND_DOWN)
    if metres == 0:
        height = "at the farm's elevation"
    else:
        height = f"{metres} m {'higher' if difference > 0 else 'lower'} than the farm"
    if over:
        return f"{away}, but {height} (over {ELEVATION_BAND_M} m): down-ranked", True
    return f"{away}, {height}", False


def rank(
    stations: Iterable[tuple[str, Station]], point: Point, farm_elevation: Decimal | None
) -> list[Candidate]:
    """`(provider, station)` pairs as ranked candidates, each with its reason."""
    candidates = []
    for provider, station in stations:
        distance = great_circle_m(point, Point(station.latitude, station.longitude))
        difference = (
            None
            if farm_elevation is None or station.elevation_m is None
            else Decimal(station.elevation_m) - Decimal(farm_elevation)
        )
        reason, down = _explain(distance, difference, farm_elevation is not None, station)
        candidates.append(Candidate(provider, station, distance, difference, down, reason))
    return sorted(
        candidates,
        key=lambda c: (c.down_ranked, c.distance_m, c.provider, c.station.provider_station_id),
    )


def _serves(provider: WeatherProvider, country: str | None) -> bool:
    countries = provider.capabilities().countries
    return countries is None or (country is not None and country in countries)


def select(
    providers: Sequence[WeatherProvider],
    point: Point,
    country: str | None,
    farm_elevation: Decimal | None,
    *,
    live: bool = True,
) -> Selection:
    notes: list[str] = []
    found: dict[tuple[str, str], tuple[str, Station]] = {}
    forecast: list[Source] = []
    backfill: list[Source] = []
    for provider in sorted(providers, key=lambda p: p.name):
        capabilities = provider.capabilities()
        if not _serves(provider, country):
            continue
        if capabilities.backfill:
            backfill.append(Source(provider.name, "History for the farm's point, before stations"))
        if capabilities.forecast:
            forecast.append(Source(provider.name, "Model forecast for the farm's point"))
        if not capabilities.observations:
            continue
        if capabilities.live_only and not live:
            notes.append(f"{provider.name}: live only, not asked in replay")
            continue
        try:
            for box in search_boxes(point):
                for station in provider.discover_stations(box):
                    found[(provider.name, station.provider_station_id)] = (provider.name, station)
        except NotConfigured:
            notes.append(f"{provider.name}: not configured")
        except Exception as exc:  # one provider down never blocks the others or the pin
            notes.append(f"{provider.name}: station list unavailable ({type(exc).__name__})")
    nearby = [
        pair
        for pair in found.values()
        if great_circle_m(point, Point(pair[1].latitude, pair[1].longitude)) <= SEARCH_RADIUS_M
    ]
    if not nearby:
        notes.append(f"No station within {SEARCH_RADIUS_M // 1000} km of the farm")
    if not forecast:
        notes.append("No forecast provider is installed for this country")
    if not backfill:
        notes.append(
            "No backfill provider is installed (ERA5 through Copernicus comes later); "
            "history is limited to the stations' own records"
        )
    return Selection(
        stations=tuple(rank(nearby, point, farm_elevation)),
        forecast=tuple(forecast),
        backfill=tuple(backfill),
        notes=tuple(notes),
    )
