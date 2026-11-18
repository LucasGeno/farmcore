"""Optional address search for the location step (Nominatim or Photon).

Off unless `GEOCODER_BASE_URL` names a server. A Nominatim server is the base URL
(`https://nominatim.example`); a Photon server is its API URL ending in `/api`. A request
carries a descriptive `User-Agent`
(the FarmCore version and the installation's public URL), asks for at most five results, and
never runs on every keystroke: the page searches when the owner submits the address form.
Nominatim's usage policy asks for one request per second and no client-side autocomplete.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from farmcore import __version__

TIMEOUT_SECONDS = 5.0
MIN_INTERVAL_SECONDS = 1.0  # Nominatim: at most one request per second per client
_last_request_at = 0.0


class GeocoderError(ValueError):
    """The configured geocoder failed; the message is safe to show the owner."""


@dataclass(frozen=True)
class Place:
    label: str
    latitude: float
    longitude: float


def user_agent(public_base_url: str) -> str:
    site = public_base_url.strip() or "self-hosted"
    return f"farmcore/{__version__} (+{site})"


def _balanced(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _nominatim(document: Any) -> list[Place]:
    if not isinstance(document, list):
        raise GeocoderError("The address search returned an unexpected response.")
    places = []
    for item in document:
        if not isinstance(item, dict):
            continue
        latitude, longitude = _balanced(item.get("lat")), _balanced(item.get("lon"))
        label = item.get("display_name")
        if latitude is not None and longitude is not None and isinstance(label, str):
            places.append(Place(label, latitude, longitude))
    return places[:5]


def _photon(document: Any) -> list[Place]:
    features = document.get("features") if isinstance(document, dict) else None
    if not isinstance(features, list):
        raise GeocoderError("The address search returned an unexpected response.")
    places = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        longitude, latitude = _balanced(coordinates[0]), _balanced(coordinates[1])
        properties = feature.get("properties") or {}
        parts = [
            properties.get("name"),
            properties.get("street"),
            properties.get("city"),
            properties.get("country"),
        ]
        label = ", ".join(part for part in parts if isinstance(part, str) and part.strip())
        if latitude is not None and longitude is not None and label:
            places.append(Place(label, latitude, longitude))
    return places[:5]


def search(
    base_url: str, query: str, *, public_base_url: str, timeout: float = TIMEOUT_SECONDS
) -> list[Place]:
    """Up to five places for `query`; raises `GeocoderError` on an upstream or shape failure."""
    text = query.strip()
    if not base_url or not text:
        return []
    global _last_request_at
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()
    headers = {"User-Agent": user_agent(public_base_url), "Accept": "application/json"}
    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            if urlsplit(base_url).path.rstrip("/").endswith("/api"):
                response = client.get(
                    base_url,
                    params={"q": text, "limit": 5},
                )
            else:
                response = client.get(
                    f"{base_url.rstrip('/')}/search",
                    params={"q": text, "format": "jsonv2", "limit": 5, "addressdetails": 0},
                )
            response.raise_for_status()
            document = response.json()
    except httpx.HTTPError as exc:
        raise GeocoderError("The address search is unavailable right now.") from exc
    except ValueError as exc:
        raise GeocoderError("The address search returned an unexpected response.") from exc
    return _photon(document) if isinstance(document, dict) else _nominatim(document)
