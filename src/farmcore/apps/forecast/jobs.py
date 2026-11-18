"""Bounded live receipts, never live requests during replay.

`fetch_live` (the manifest job) only plans: in live mode it queues one durable task per due farm
(`core/tasks.py`). `farmcore worker` runs `fetch_farm` for each: no transaction is open during
the request, and the receipt and health are written only if the attempt is still current.

The current API does not expose a model issue time. The legacy non-null issued_at
column is a receipt identity for this source ONLY; props and presentation explicitly
label it as such. available_at and fetched_at are wall time after receipt, never
an inferred model issue time. Saved as-issued sources keep their existing semantics.
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import func, or_, select

from farmcore.apps.forecast import provider
from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.provider import _finite
from farmcore.apps.forecast.sources import UNITS, VARIABLES, Run
from farmcore.apps.forecast.store import RECEIPT, import_runs
from farmcore.core import tasks
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Asset, Farm
from farmcore.core.weather.contract import Fetched, Point
from farmcore.core.weather.store import receive
from farmcore.db import session_scope

logger = logging.getLogger(__name__)

JOB_NAME = "forecast.fetch_live"
SOURCE = "open-meteo-live-receipt"
CACHE = timedelta(hours=1)
MAX_FARMS = 5


def fetch_live(ctx: JobContext) -> None:
    """Queue a receipt fetch for each farm due (never attempted, cache expired or unreadable
    health), at most MAX_FARMS, least recently attempted first, skipping farms whose fetch is in
    flight. No network here: a request handler or tick that runs this job only enqueues."""
    if not isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        return
    now = datetime.now(UTC)
    last_attempt = Farm.props["forecast_health"]["last_attempt"].astext
    farms = (
        select(Farm.id)
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
    )
    if ctx.farm_ids is not None:
        farms = farms.where(Farm.id.in_(ctx.farm_ids))
    busy = tasks.in_flight(ctx.session, JOB_NAME)
    if busy:
        farms = farms.where(Farm.id.not_in(busy))
    for farm_id in ctx.session.scalars(farms).all():
        tasks.enqueue(ctx.session, JOB_NAME, farm_id)


def _location(session, farm: Farm):
    location = farm.props.get("forecast_location")
    if location is None:
        point = session.execute(
            select(func.ST_Y(Asset.geom), func.ST_X(Asset.geom))
            .where(Asset.farm_id == farm.id, Asset.kind == "station", Asset.geom.is_not(None))
            .order_by(Asset.id)
            .limit(1)
        ).first()
        location = {"latitude": point[0], "longitude": point[1]} if point else None
    return location


def fetch_farm(ctx: tasks.TaskContext) -> None:
    """One farm's live receipt: read its location, request within the attempt's deadline with no
    transaction open, then write the receipt or the failure only if the attempt is current."""
    with session_scope(ctx.session_factory) as session:
        farm = session.get(Farm, ctx.farm_id)
        if farm is None:
            return
        location = _location(session, farm)
    attempted = datetime.now(UTC)
    receipt = fetched = error = None
    open_meteo = None
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
        timeout = min(ctx.settings.forecast_fetch_timeout_seconds, ctx.attempt.remaining())
        open_meteo = provider.provider(ctx.settings, timeout)
        fetched = open_meteo.fetch_forecast(
            Point(location["latitude"], location["longitude"]), (), None
        )
        doc = json.loads(fetched.payload)
        values = provider.legacy_values(fetched)
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
        received = fetched.fetched_at
        receipt = Run(
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
                "request_url": fetched.request.url,
                "endpoint_kind": fetched.request.endpoint_kind,
                "coverage": coverage,
                "coverage_start": min(t for t, _, _ in values).isoformat(),
                "coverage_end": max(t for t, _, _ in values).isoformat(),
                "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
            },
        )
    except Exception as exc:  # any failure is recorded, never a silent re-plan every deadline
        error = type(exc).__name__
    try:
        _publish(ctx, attempted, receipt, error, (open_meteo, fetched))
    except Exception as exc:
        # The receipt could not be written: record that instead, under the same fence, so the
        # farm waits for the cache like any failed attempt.
        logger.error(
            "forecast receipt for farm %s not written (%s)", ctx.farm_id, type(exc).__name__
        )
        _publish(ctx, attempted, None, type(exc).__name__)


def _publish(
    ctx: tasks.TaskContext,
    attempted: datetime,
    receipt: Run | None,
    error,
    response: tuple[provider.OpenMeteo, Fetched] | tuple[None, None] = (None, None),
) -> None:
    """Write the receipt and health, or the failure, only if the attempt is still current. A new
    receipt is written twice: the provider's response into the normalised schema (`receive`),
    then the forecast run that links to it."""
    with session_scope(ctx.session_factory) as session:
        if not tasks.publish_if_current(session, ctx.attempt):
            return
        farm = session.get(Farm, ctx.farm_id, with_for_update=True, populate_existing=True)
        health = farm.props.get("forecast_health")
        health = health if isinstance(health, dict) else {}
        if receipt is None:
            health = {**health, "last_attempt": attempted.isoformat(), "error": error}
        else:
            duplicate = session.scalar(
                select(ForecastRun.id)
                .where(
                    ForecastRun.farm_id == farm.id,
                    ForecastRun.source == SOURCE,
                    ForecastRun.props["response_hash"].astext == receipt.props["response_hash"],
                )
                .limit(1)
            )
            if not duplicate:
                weather = receive(session, response[0], response[1], farm_id=farm.id)
                receipt = replace(receipt, props={**receipt.props, RECEIPT: weather.id})
            import_runs(session, farm.id, [] if duplicate else [receipt])
            health = {
                "last_attempt": attempted.isoformat(),
                "last_success": receipt.available_at.isoformat(),
            }
        farm.props = {**farm.props, "forecast_health": health}
