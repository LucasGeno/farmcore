"""`weather_meteofrance.ingest`: live observations of each farm's Météo-France station.

Live mode only (replay never calls the network). With portal credentials set, the job queues
each due farm whose weather station has a Météo-France number (`meteofrance` external key, as
the stations seed gives the demo station), and `farmcore worker` runs `ingest_farm`:

1. read the hours the farm holds for that station in the last week (no transaction stays open
   during the requests);
2. fetch the windows `plan` names, the recent one first, within the attempt's deadline; each
   stands alone, so a failing archive never holds back the live hours (`plan` finds the
   archive gap again on the next run from the first missing hour);
3. only if the attempt is current: store each response (legacy `observation` rows, which older
   readers still read, and the normalised receipt and observations, under the replay's station,
   `stations.connector.EXT_SOURCE`) and the station's `meteofrance_live` health:
   `{last_attempt, error, archived_through}`: each failed step and its HTTP status (never a
   credential), and the end of the last archive window answered, never ordered again.

Known limits: the normalised station and its (station, variable, period) key are shared by
every farm holding that station number, so a second farm with the same station sees the first
farm's hours as held (the replay has the same limit; per-farm merging is the merge layer's).
DPObs hours stay `unchecked`: the archive only fills missing hours and never upgrades a held
hour's QC. Legacy `observation` rows are upserted (the last writer wins) while the normalised
store keeps the first receipt, so they can differ where live and replay hours overlap.
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


def plan(
    held: Collection[datetime], now: datetime, archived_through: datetime | None = None
) -> list[TimeRange]:
    """The windows a run fetches, oldest first, each `(start, end]` on whole hours: the
    archive from just before the first hour the farm lacks (`held`: the ends of the hours it
    holds) in the week before the recent window and after `archived_through` (the archive was
    already asked up to it: an hour it lacked is not ordered again), if any; then the recent
    24 hours after the newest hour held in them (DPObs answers one hour per call). A gap left
    inside the recent window is ordered from the archive once it is older than 24 hours."""
    end = now.replace(minute=0, second=0, microsecond=0)
    newest = max((h for h in held if end - RECENT < h <= end), default=end - RECENT)
    recent = TimeRange(newest, end)
    archive_end = end - RECENT
    hour = end - BACKFILL + HOUR
    if archived_through is not None:
        hour = max(hour, archived_through + HOUR)
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


def _held(session: Session, farm_id: int, now: datetime) -> tuple[Asset, Station, set] | None:
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
    return asset, station, set(held)


def _archived_through(health: object) -> datetime | None:
    try:
        return datetime.fromisoformat(health["archived_through"])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return None


def ingest_farm(ctx: tasks.TaskContext) -> None:
    """The durable task: fetch outside any transaction, store only if the attempt is current.
    The recent window is fetched first and each window stands alone: a failing archive never
    holds back the live hours, and its gap is ordered again on the next run."""
    with session_scope(ctx.session_factory) as session:
        now = get_clock(session, ctx.settings).now()
        found = _held(session, ctx.farm_id, now)
        if found is None:
            return
        asset, station, held = found
        asset_id, health = asset.id, (asset.props or {}).get(HEALTH)
    archived_through = _archived_through(health)
    live = provider.provider(ctx.settings, timeout=ctx.attempt.remaining())
    responses: list[Fetched] = []
    errors = []
    for window in reversed(plan(held, now, archived_through)):
        try:
            responses.append(live.fetch_observations(station, window))
        except provider.PortalError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:  # recorded, never raised into a retry loop with a stale plan
            errors.append(type(exc).__name__)
            continue
        if window.end <= now - RECENT:
            archived_through = window.end
    error = "; ".join(errors) or None
    if error is not None:
        logger.warning("%s farm %s: %s", JOB_NAME, ctx.farm_id, error)
    with session_scope(ctx.session_factory) as session:
        if not tasks.publish_if_current(session, ctx.attempt):
            return
        asset = session.get(Asset, asset_id, with_for_update=True)
        for fetched in responses:
            store(session, asset, live, station, fetched)
        asset.props = {
            **(asset.props or {}),
            HEALTH: {
                "last_attempt": datetime.now(UTC).isoformat(),
                "error": error,
                "archived_through": archived_through.isoformat() if archived_through else None,
            },
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
