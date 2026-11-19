"""Bounded public CAP lookups. Only geographic filters leave the installation."""

import asyncio
import json
import math

import httpx
from shapely.errors import ShapelyError
from starlette.concurrency import run_in_threadpool

# Assumed product limits: one small map window, bounded bytes and total wall time.
MAX_BYTES = 2_000_000
DEADLINE_SECONDS = 8
MAX_SPAN = 0.02
MAX_DISTANCE = 0.1
HOSTS = frozenset({"apicarto.ign.fr", "sigpac-hubcloud.es"})
FALLBACK = "Public registry unavailable here. Import a file instead."


class Unavailable(ValueError):
    pass


def bounding_box(value, point):
    try:
        bounds = tuple(float(v) for v in value.split(","))
        west, south, east, north = bounds
        lon, lat = point["longitude"], point["latitude"]
        if not all(math.isfinite(v) for v in (*bounds, lon, lat)) or not (
            -180 <= west < east <= 180
            and -90 < south < north < 90
            and east - west <= MAX_SPAN
            and north - south <= MAX_SPAN
            and max(abs(west - lon), abs(east - lon), abs(south - lat), abs(north - lat))
            <= MAX_DISTANCE
        ):
            raise ValueError
        return bounds
    except (ValueError, TypeError, KeyError):
        raise Unavailable(
            "Zoom in near your saved farm location, or import a file instead."
        ) from None


async def document(client, url, params):
    target = httpx.URL(url)
    if target.scheme != "https" or target.host not in HOSTS:
        raise Unavailable(FALLBACK)
    async with client.stream("GET", url, params=params) as response:
        # No redirects, including same-host ones. Never follow pagination links either.
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise Unavailable(FALLBACK)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_BYTES:
                raise Unavailable(FALLBACK)
            body.extend(chunk)
    data = await run_in_threadpool(json.loads, body)
    features = data["features"]
    if data["type"] != "FeatureCollection" or not isinstance(features, list):
        raise Unavailable(FALLBACK)
    if len(features) > 200 or data.get("numberMatched", len(features)) != len(features):
        raise Unavailable("Too many public parcels. Zoom in, or import a file instead.")
    if any(link.get("rel") == "next" for link in data.get("links", [])):
        raise Unavailable("Too many public parcels. Zoom in, or import a file instead.")
    return data


async def lookup(country, bbox):
    from . import es_sigpac, fr_rpg

    connector = {"FR": fr_rpg, "ES": es_sigpac}.get(country)
    if connector is None:
        raise Unavailable(FALLBACK)
    try:
        # Covers connection, headers, all body chunks and both Spanish requests together.
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                timeout=DEADLINE_SECONDS,
                follow_redirects=False,
                trust_env=False,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                return await connector.fetch(client, bbox)
    except Unavailable:
        raise
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        RecursionError,
        ShapelyError,
    ) as exc:
        raise Unavailable(FALLBACK) from exc
