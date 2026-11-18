"""Location-step rules: read the submitted point, resolve it offline, store the owner's choice.

Nothing is guessed for a sea or border point: the form re-renders with a question and the owner
supplies the time zone and country explicitly. The farm's `props` keep the coordinates as
`forecast_location` (the forecast job's existing input), plus `time_zone` and `country`.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    props[LOCATION_KEY] = {"latitude": choice.latitude, "longitude": choice.longitude}
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
