"""`weather_meteofrance.ingest`: live observations of each farm's Météo-France station.

Live mode only (replay never calls the network). With portal credentials set, the job queues
each due farm whose weather station has a Météo-France number (`meteofrance` external key, as
the stations seed gives the demo station), and `farmcore worker` runs `ingest_farm`:

1. read the hours the farm holds for that station in the last week (no transaction stays open
   during the requests);
2. fetch the windows `plan` names, oldest first, within the attempt's deadline; a failed window
   ends the run, so nothing newer is stored over a gap the archive has not filled yet;
3. only if the attempt is current: store each response (legacy `observation` rows, which older
   readers still read, and the normalised receipt and observations, under the replay's station,
   `stations.connector.EXT_SOURCE`) and the station's `meteofrance_live` health:
   `{last_attempt, error}`, the error being the step and HTTP status, never a credential.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.stations import connector, service
from farmcore.apps.stations.provider import station_of
from farmcore.apps.weather_meteofrance import provider
from farmcore.core import tasks
from farmcore.core.access import repo_for_job
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Asset, ExtId
from farmcore.core.models.weather import WeatherObservation
from farmcore.core.weather.contract import Fetched, Station, TimeRange
from farmcore.core.weather.store import record_receipt, store_observations, upsert_station
from farmcore.core.weather.variables import VARIABLES
from farmcore.db import session_scope

logger = logging.getLogger(__name__)

JOB_NAME = "weather_meteofrance.ingest"
INTERVAL = timedelta(hours=1)
# One run: a token, up to 24 DPObs calls and an archive order with its polls.
DEADLINE = timedelta(minutes=3)
HEALTH = "meteofrance_live"

HOUR = timedelta(hours=1)
# DPObs serves the last 24 hours only; older hours come from the DPClim archive.
RECENT = timedelta(hours=24)
# How far back a farm's missing hours are fetched (assumed: a week covers a worker outage
# without ordering a season of archive on a farm's first run).
BACKFILL = timedelta(days=7)


def plan(held: Collection[datetime], now: datetime) -> list[TimeRange]:
    """The windows a run fetches, oldest first, each `(start, end]` on whole hours: the
    archive from just before the first hour the farm lacks (`held`: the ends of the hours it
    holds) in the week before the recent window, if any; then the recent 24 hours after the
    newest hour held in them (DPObs answers one hour per call). A gap left inside the recent
    window is filled from the archive once it is older than 24 hours."""
    end = now.replace(minute=0, second=0, microsecond=0)
    newest = max((h for h in held if end - RECENT < h <= end), default=end - RECENT)
    recent = TimeRange(newest, end)
    archive_end = end - RECENT
    hour = end - BACKFILL + HOUR
    while hour <= archive_end and hour in held:
        hour += HOUR
    windows = [TimeRange(hour - HOUR, archive_end)] if hour <= archive_end else []
    return windows + ([recent] if recent.start < end else [])


def ingest(ctx: JobContext) -> None:
    """Live only, with credentials: queue each due farm that has a Météo-France station."""
    settings = ctx.settings
    if not (settings.meteofrance_client_id and settings.meteofrance_client_secret):
        return
    if not isinstance(get_clock(ctx.session, settings), LiveClock):
        return
    farms = (
        select(Asset.farm_id)
        .join(ExtId, (ExtId.model == "asset") & (ExtId.res_id == Asset.id))
        .where(Asset.kind == "station", ExtId.source == connector.EXT_SOURCE)
        .distinct()
        .order_by(Asset.farm_id)
    )
    farm_ids = [f for f in ctx.session.scalars(farms) if ctx.farm_ids is None or f in ctx.farm_ids]
    tasks.enqueue_due(ctx.session, JOB_NAME, INTERVAL, farm_ids)


def _held(session: Session, farm_id: int, now: datetime) -> tuple[int, Station, set] | None:
    repo = repo_for_job(session, farm_id, JOB_NAME)
    asset = service.station_for_farm(repo)
    if asset is None:
        return None
    try:
        station = station_of(session, asset)
    except ValueError:
        return None  # the farm's station is not a Météo-France one
    held = repo.execute(
        select(WeatherObservation.period_end)
        .where(
            WeatherObservation.station_id.in_(service.weather_stations_of(asset.id)),
            WeatherObservation.period_end > now - BACKFILL - RECENT,
        )
        .distinct(),
        scope=service.SCOPE,
        resource="weather_observation",
    ).scalars()
    return asset.id, station, set(held)


def ingest_farm(ctx: tasks.TaskContext) -> None:
    """The durable task: fetch outside any transaction, store only if the attempt is current."""
    with session_scope(ctx.session_factory) as session:
        now = get_clock(session, ctx.settings).now()
        found = _held(session, ctx.farm_id, now)
    if found is None:
        return
    asset_id, station, held = found
    live = provider.provider(ctx.settings, timeout=ctx.attempt.remaining())
    responses: list[Fetched] = []
    error = None
    try:
        for window in plan(held, now):
            responses.append(live.fetch_observations(station, window))
    except provider.PortalError as exc:
        error = str(exc)
    except Exception as exc:  # recorded, never raised into a retry loop with a stale plan
        error = type(exc).__name__
    if error is not None:
        logger.warning("%s farm %s: %s", JOB_NAME, ctx.farm_id, error)
    with session_scope(ctx.session_factory) as session:
        if not tasks.publish_if_current(session, ctx.attempt):
            return
        asset = session.get(Asset, asset_id, with_for_update=True)
        for fetched in responses:  # the windows before any failure
            store(session, asset, live, station, fetched)
        asset.props = {
            **asset.props,
            HEALTH: {"last_attempt": datetime.now(UTC).isoformat(), "error": error},
        }


def store(
    session: Session,
    asset: Asset,
    live: provider.MeteoFranceLive,
    station: Station,
    fetched: Fetched,
) -> None:
    """One response: legacy rows for the asset, then its receipt (this provider, the asset's
    farm) and observations under the replay's station."""
    observations = list(fetched.values)
    connector.upsert(
        session,
        asset.id,
        [(v.period_end, VARIABLES[v.variable].legacy_name, float(v.value)) for v in observations],
    )
    receipt = record_receipt(
        session,
        provider=live.name,
        licence=live.licence(),
        fetched=fetched,
        secret_fields=[f.name for f in live.auth_schema() if f.secret],
        farm_id=asset.farm_id,
    )
    if observations:
        weather_station = upsert_station(session, connector.EXT_SOURCE, station)
        store_observations(session, receipt, weather_station, observations)
