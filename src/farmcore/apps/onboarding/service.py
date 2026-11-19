"""Location-step rules: read the submitted point, resolve it offline, store the owner's choice.

Nothing is guessed for a sea or border point: the form re-renders with a question and the owner
supplies the time zone and country explicitly. The farm's `props` keep the coordinates as
`forecast_location` (the forecast job's existing input), plus `time_zone` and `country`.

Weather sources: placing the pin runs `selection.select` over the providers of the active apps
and keeps the result in `props["weather_sources"]`: the ranked stations with their reasons, the
forecast and backfill providers and notes. The owner can override the station and forecast
choices and give the farm's elevation (`props["elevation_m"]`, never derived), which re-ranks
the stored stations without asking a provider again. Moving the pin drops both, since they
described the old point.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from farmcore.core.farm_location import (
    COUNTRY_KEY,
    COUNTRY_NAME_KEY,
    LOCATION_KEY,
    TIME_ZONE_KEY,
    Located,
    country_name,
    farm_country,
    farm_location,
    locate,
    valid_coordinate,
    valid_time_zone,
)
from farmcore.core.models.canonical import Farm
from farmcore.core.weather.contract import Point, Station, WeatherProvider
from farmcore.core.weather.selection import Candidate, Selection, rank, select

SOURCES_KEY = "weather_sources"
ELEVATION_KEY = "elevation_m"
# Bounds of a typed farm elevation, metres: the Dead Sea shore to above any farmed land.
ELEVATION_RANGE = (Decimal(-450), Decimal(6000))


@dataclass(frozen=True)
class Choice:
    """The values the owner accepted: the derived facts or their explicit answer."""

    latitude: float
    longitude: float
    time_zone: str
    country: str
    country_name: str


class LocationError(ValueError):
    """The form input is unusable; the message is plain words for the owner."""


def parse_point(latitude: str, longitude: str) -> tuple[float, float]:
    try:
        lat, lon = float(latitude.strip()), float(longitude.strip())
    except (AttributeError, ValueError):
        raise LocationError("Enter the latitude and longitude as decimal numbers.") from None
    if not valid_coordinate(lat, lon):
        raise LocationError(
            "Enter a latitude between -90 and 90 and a longitude between -180 and 180."
        )
    return lat, lon


def manual_facts(time_zone: str, country: str) -> tuple[str, str, str] | None:
    """`(time_zone, ISO country, country name)` when the owner supplied both, else None; an
    incomplete or unknown choice raises `LocationError`."""
    zone = (time_zone or "").strip()
    code = (country or "").strip().upper()
    if not zone and not code:
        return None
    if not zone or not code:
        raise LocationError("Choose both the time zone and the country for this point.")
    if not valid_time_zone(zone):
        raise LocationError("Choose a time zone the installation knows, for example Europe/Paris.")
    name = country_name(code)
    if name is None:
        raise LocationError("Choose a known country code, for example FR.")
    return zone, code, name


def choose(
    latitude: float,
    longitude: float,
    *,
    time_zone: str = "",
    country: str = "",
    manual: bool = False,
) -> tuple[Choice | None, Located]:
    """The owner-confirmed facts, or None when the offline lookup needs an answer. `manual`
    makes the submitted time zone and country authoritative (the owner's explicit choice);
    otherwise the point is derived afresh, so moving a point never keeps stale facts. The
    second value is the lookup, so the form can show its proposed values."""
    located = locate(latitude, longitude)
    explicit = manual_facts(time_zone, country) if manual else None
    if manual and explicit is None:
        raise LocationError("Choose both the time zone and the country for this point.")
    if explicit is not None:
        zone, code, name = explicit
        return Choice(latitude, longitude, zone, code, name), located
    if located.question:
        return None, located
    if located.time_zone is None or located.country is None:  # defensive: ask, never guess
        return None, located
    return (
        Choice(latitude, longitude, located.time_zone, located.country, located.country_name or ""),
        located,
    )


def store(farm: Farm, choice: Choice) -> None:
    """Write the coordinates, time zone and country into the farm's `props`."""
    props: dict[str, Any] = dict(farm.props or {})
    point = {"latitude": choice.latitude, "longitude": choice.longitude}
    if props.get(LOCATION_KEY) != point:  # the old point's elevation and overrides are stale
        props.pop(ELEVATION_KEY, None)
        props.pop(SOURCES_KEY, None)
    props[LOCATION_KEY] = point
    props[TIME_ZONE_KEY] = choice.time_zone
    props[COUNTRY_KEY] = choice.country
    props[COUNTRY_NAME_KEY] = choice.country_name
    farm.props = props


def current(farm: Farm) -> dict[str, Any]:
    """The form's starting values."""
    point = farm_location(farm) or {}
    return {
        "latitude": point.get("latitude", ""),
        "longitude": point.get("longitude", ""),
        "time_zone": (farm.props or {}).get(TIME_ZONE_KEY, ""),
        "country": farm_country(farm) or "",
    }


def farm_elevation(farm: Farm) -> Decimal | None:
    value = (farm.props or {}).get(ELEVATION_KEY)
    return None if value is None else Decimal(str(value))


def _candidate_props(c: Candidate) -> dict[str, Any]:
    elevation = c.station.elevation_m
    difference = c.elevation_difference_m
    return {
        "provider": c.provider,
        "id": c.station.provider_station_id,
        "name": c.station.name,
        "latitude": c.station.latitude,
        "longitude": c.station.longitude,
        "elevation_m": None if elevation is None else str(elevation),
        "distance_m": round(c.distance_m),
        "elevation_difference_m": None if difference is None else str(difference),
        "down_ranked": c.down_ranked,
        "reason": c.reason,
    }


def _store_ranked(farm: Farm, sources: dict[str, Any], ranked: Sequence[Candidate]) -> None:
    props = dict(farm.props or {})
    props[SOURCES_KEY] = {**sources, "stations": [_candidate_props(c) for c in ranked]}
    farm.props = props


def choose_sources(farm: Farm, providers: Sequence[WeatherProvider]) -> Selection | None:
    """Select the farm's weather sources from its stored point and country (None without a
    point) and keep them, with any override the owner made for this point."""
    point = farm_location(farm)
    if point is None:
        return None
    chosen = select(
        providers,
        Point(point["latitude"], point["longitude"]),
        farm_country(farm),
        farm_elevation(farm),
    )
    previous = (farm.props or {}).get(SOURCES_KEY) or {}
    sources = {
        "forecast": [{"provider": f.provider, "reason": f.reason} for f in chosen.forecast],
        "backfill": [{"provider": b.provider, "reason": b.reason} for b in chosen.backfill],
        "notes": list(chosen.notes),
        "override": previous.get("override", {}),
    }
    _store_ranked(farm, sources, chosen.stations)
    return chosen


def sources_of(farm: Farm) -> dict[str, Any] | None:
    """The stored choice with what is in effect: the owner's override, else the first ranked
    station and the first forecast provider."""
    sources = (farm.props or {}).get(SOURCES_KEY)
    if not sources:
        return None
    override = sources.get("override") or {}
    stations, forecast = sources.get("stations", []), sources.get("forecast", [])
    station = next(
        (s for s in stations if f"{s['provider']}:{s['id']}" == override.get("station")),
        stations[0] if stations else None,
    )
    model = next(
        (f for f in forecast if f["provider"] == override.get("forecast")),
        forecast[0] if forecast else None,
    )
    return {
        **sources,
        "station": station,
        "station_overridden": "station" in override,
        "model": model,
        "model_overridden": "forecast" in override,
        "farm_elevation_m": farm_elevation(farm),
    }


def parse_elevation(text: str) -> Decimal | None:
    value = (text or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        elevation = Decimal(value)
    except InvalidOperation:
        raise LocationError("Enter the farm's elevation in metres, for example 120.") from None
    low, high = ELEVATION_RANGE
    if not elevation.is_finite() or not low <= elevation <= high:
        raise LocationError(f"Enter an elevation between {low} and {high} metres.")
    return elevation


def override_sources(farm: Farm, *, station: str, forecast: str, elevation: str) -> None:
    """The owner's choices: a listed station (`provider:id`) and forecast provider, or "" for the
    automatic one, and the farm's elevation ("" when unknown), which re-ranks the stations."""
    sources = (farm.props or {}).get(SOURCES_KEY)
    if not sources:
        raise LocationError("Save the farm's location first.")
    stations = sources.get("stations", [])
    keys = {f"{s['provider']}:{s['id']}" for s in stations}
    models = {f["provider"] for f in sources.get("forecast", [])}
    if station and station not in keys:
        raise LocationError("Choose one of the listed stations.")
    if forecast and forecast not in models:
        raise LocationError("Choose one of the listed forecasts.")
    height = parse_elevation(elevation)
    props = dict(farm.props or {})
    if height is None:
        props.pop(ELEVATION_KEY, None)
    else:
        props[ELEVATION_KEY] = float(height) if height % 1 else int(height)
    farm.props = props
    point = farm_location(farm) or {}
    ranked = rank(
        [
            (
                s["provider"],
                Station(
                    provider_station_id=s["id"],
                    name=s["name"],
                    latitude=s["latitude"],
                    longitude=s["longitude"],
                    elevation_m=None if s["elevation_m"] is None else Decimal(s["elevation_m"]),
                ),
            )
            for s in stations
        ],
        Point(point["latitude"], point["longitude"]),
        height,
    )
    chosen = {"station": station, "forecast": forecast}
    _store_ranked(farm, {**sources, "override": {k: v for k, v in chosen.items() if v}}, ranked)
