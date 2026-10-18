"""Bounded live receipts, never live requests during replay.

The current API does not expose a model issue time. The legacy non-null issued_at
column is a receipt identity for this source ONLY; props and presentation explicitly
label it as such. available_at and fetched_at are wall time after receipt, never
an inferred model issue time. Saved as-issued sources keep their existing semantics.
"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, or_, select

from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.sources import UNITS, VARIABLES, Run, _utc, parse_hourly
from farmcore.apps.forecast.store import import_runs
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Asset, Farm

JOB_NAME = "forecast.fetch_live"
SOURCE = "open-meteo-live-receipt"
CACHE = timedelta(hours=1)
MAX_FARMS = 5


def _finite(value) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def _validated_values(doc, now):
    if not isinstance(doc, dict) or doc.get("utc_offset_seconds") != 0:
        raise ValueError("UTC response required")
    hourly, units = doc["hourly"], doc["hourly_units"]
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise ValueError("hourly data and units must be objects")
    for key, bound in (("latitude", 90), ("longitude", 180)):
        value = doc.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _finite(value)
            or abs(value) > bound
        ):
            raise ValueError("invalid provider location")
    if not isinstance(hourly.get("time"), list):
        raise ValueError("hourly time array required")
    if any(not isinstance(t, str) for t in hourly["time"]):
        raise ValueError("hourly timestamps must be strings")
    times = [_utc(t) for t in hourly["time"]]
    if not 1 <= len(times) <= 6 * 24 or any(
        b - a != timedelta(hours=1) for a, b in zip(times, times[1:], strict=False)
    ):
        raise ValueError("bounded consecutive hourly values required")
    if times[-1] <= now or times[-1] > now + timedelta(days=7):
        raise ValueError("forecast coverage outside request")
    for name in VARIABLES:
        if name not in hourly:
            continue  # missing variable is retained as missing, never zero
        if (
            not isinstance(hourly[name], list)
            or units.get(name) != UNITS[name]
            or len(hourly[name]) != len(times)
        ):
            raise ValueError("incompatible units or incomplete array")
        for value in hourly[name]:
            if value is not None and not _finite(value):
                raise ValueError("non-finite weather value")
            if value is not None and (
                (name in ("precipitation", "wind_speed_10m", "wind_gusts_10m") and float(value) < 0)
                or (name == "relative_humidity_2m" and not 0 <= float(value) <= 100)
            ):
                raise ValueError("weather value outside unit bounds")
    values = parse_hourly(hourly)
    if not values:
        raise ValueError("empty forecast")
    return values


def fetch_live(ctx: JobContext) -> None:
    if not isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        return
    now = datetime.now(UTC)
    last_attempt = Farm.props["forecast_health"]["last_attempt"].astext
    farms = (
        select(Farm)
        .where(
            or_(
                last_attempt.is_(None),
                last_attempt <= (now - CACHE).isoformat(),
                last_attempt > now.isoformat(),
                ~last_attempt.regexp_match(r"^\d{4}-\d{2}-\d{2}T"),
            )
        )
        .order_by(last_attempt.asc().nulls_first(), Farm.id)
        .limit(MAX_FARMS)
        .with_for_update()
    )
    if ctx.farm_ids is not None:
        farms = farms.where(Farm.id.in_(ctx.farm_ids))
    for farm in ctx.session.scalars(farms):
        health = farm.props.get("forecast_health")
        health = health if isinstance(health, dict) else {}
        location = farm.props.get("forecast_location")
        if location is None:
            point = ctx.session.execute(
                select(func.ST_Y(Asset.geom), func.ST_X(Asset.geom))
                .where(Asset.farm_id == farm.id, Asset.kind == "station", Asset.geom.is_not(None))
                .order_by(Asset.id)
                .limit(1)
            ).first()
            location = {"latitude": point[0], "longitude": point[1]} if point else None
        attempted = datetime.now(UTC)
        try:
            if location is None:
                raise ValueError("location unavailable")
            if not isinstance(location, dict) or any(
                key not in location
                or isinstance(location[key], bool)
                or not _finite(location[key])
                or abs(float(location[key])) > bound
                for key, bound in (("latitude", 90), ("longitude", 180))
            ):
                raise ValueError("invalid location")
            location = {key: float(location[key]) for key in ("latitude", "longitude")}
            url = "https://api.open-meteo.com/v1/forecast?" + urlencode(
                {
                    **location,
                    "hourly": ",".join(VARIABLES),
                    "forecast_days": 6,
                    "timezone": "UTC",
                    "wind_speed_unit": "kmh",
                    "temperature_unit": "celsius",
                    "precipitation_unit": "mm",
                }
            )
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
            if len(response.content) > 256_000:
                raise ValueError("response too large")
            doc = response.json()
            received = datetime.now(UTC)
            values = _validated_values(doc, received)
            coverage = {
                variable: {
                    "known": sum(v == variable for _, v, _ in values),
                    "expected": len(doc["hourly"]["time"]),
                }
                for variable in VARIABLES.values()
            }
            fingerprint = sha256(
                json.dumps(
                    {
                        "location": location,
                        "latitude": doc.get("latitude"),
                        "longitude": doc.get("longitude"),
                        "hourly": doc["hourly"],
                        "units": doc["hourly_units"],
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            duplicate = ctx.session.scalar(
                select(ForecastRun.id)
                .where(
                    ForecastRun.farm_id == farm.id,
                    ForecastRun.source == SOURCE,
                    ForecastRun.props["response_hash"].astext == fingerprint,
                )
                .limit(1)
            )
            with ctx.session.begin_nested():
                import_runs(
                    ctx.session,
                    farm.id,
                    []
                    if duplicate
                    else [
                        Run(
                            source=SOURCE,
                            issued_at=received,
                            available_at=received,
                            label="Open-Meteo live forecast receipt",
                            as_issued=False,
                            latitude=doc.get("latitude"),
                            longitude=doc.get("longitude"),
                            values=values,
                            props={
                                "response_hash": fingerprint,
                                "provider_issued_at": None,
                                "issued_at_kind": "receipt",
                                "fetched_at": received.isoformat(),
                                "requested_location": location,
                                "units": doc.get("hourly_units", UNITS),
                                "request_url": url,
                                "coverage": coverage,
                                "coverage_start": min(t for t, _, _ in values).isoformat(),
                                "coverage_end": max(t for t, _, _ in values).isoformat(),
                                "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
                            },
                        )
                    ],
                )
            health = {"last_attempt": attempted.isoformat(), "last_success": received.isoformat()}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            health = {**health, "last_attempt": attempted.isoformat(), "error": type(exc).__name__}
        farm.props = {**farm.props, "forecast_health": health}
    ctx.session.flush()
