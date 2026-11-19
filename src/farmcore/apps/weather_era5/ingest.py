"""`weather_era5.backfill`: the season's missing history from ERA5, for each farm with a pin.

Live mode only (replay never calls the network). With a CDS key set, the job queues each due
farm that has a location (`forecast_location`, the farm pin), once a day, and `farmcore worker`
runs `backfill_farm`:

1. read which hours the farm's ERA5 grid point already holds this season (no transaction stays
   open during the requests);
2. `plan` the whole days it lacks, from 1 January to the newest day ERA5 serves, in windows of
   at most `MAX_DAYS`. A CDS order can queue for a long time, so a run orders one window and
   waits for it only until its deadline: an order still queued is kept in the farm's
   `era5_backfill` health and collected, not ordered again, on the next run. Windows are
   independent: a failed one never holds back the next;
3. only if the attempt is current: store each response under the grid-point station
   (`copernicus-era5`, a model, not a station: its receipt says `model-reanalysis`) and the
   health `{last_attempt, error, pending}`: each failed step and its HTTP status (never the
   key) and the order still waiting, if any.

Known limits: the grid point and its (station, variable, period) key are shared by every farm
within the same 0.25 degree cell, so a second farm sees the first farm's hours as held and its
receipts are not its own (per-farm merging is the merge layer's). ERA5T hours, the newest days,
are overwritten upstream by the final ERA5 about two months later; the first receipt is kept.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.stations import service
from farmcore.apps.weather_era5 import provider
from farmcore.core import tasks
from farmcore.core.access import repo_for_job
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.farm_location import farm_location
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Farm
from farmcore.core.models.weather import WeatherObservation, WeatherStation
from farmcore.core.weather.contract import Fetched, Point, Station, TimeRange
from farmcore.core.weather.store import record_receipt, store_observations, upsert_station
from farmcore.core.weather.variables import VARIABLES
from farmcore.db import session_scope

logger = logging.getLogger(__name__)

JOB_NAME = "weather_era5.backfill"
INTERVAL = timedelta(days=1)
# One run: submit and poll for a few minutes; a longer queue is collected by a later run.
DEADLINE = timedelta(minutes=3)
HEALTH = "era5_backfill"

HOUR = timedelta(hours=1)
# ERA5 is about 5 days behind real time; the catalogue's constraints ended 11 days behind on
# 2026-11-19. Assumed: 10 days, so a window is rarely refused for lying beyond the data.
LAG = timedelta(days=10)
# Assumed: a quarter keeps one order small (the CDS publishes no size limit for the dataset,
# and asks for small requests over large ones).
MAX_DAYS = 92
# A run orders at most this many windows; later ones wait for the next day's run.
MAX_WINDOWS = 3
# Assumed: an order not finished after a day is abandoned and ordered again.
MAX_PENDING = timedelta(days=1)
# The share of the attempt's deadline the requests may use: storing is fenced by the same
# deadline (`publish_if_current`), so the rest is left to write the receipts and the health.
FETCH_SHARE = 0.8
GRID = 0.25  # degrees: the timeseries dataset's grid


def season_start(now: datetime) -> datetime:
    """Where the history begins: 1 January of the current year (hydrothermal time for crop
    models counts from it)."""
    return datetime(now.year, 1, 1, tzinfo=UTC)


def grid_station(point: Point) -> Station:
    """The ERA5 grid point nearest `point`, as the station its hours are stored under."""
    latitude = round(round(point.latitude / GRID) * GRID, 2)
    longitude = round(round(point.longitude / GRID) * GRID, 2)
    return Station(
        provider_station_id=f"{latitude:.2f},{longitude:.2f}",
        name=f"ERA5 grid point {latitude:.2f}, {longitude:.2f} (model reanalysis)",
        latitude=latitude,
        longitude=longitude,
    )


def plan(
    held: Collection[datetime], now: datetime, start: datetime, max_days: int = MAX_DAYS
) -> list[TimeRange]:
    """The windows to order, oldest first, each `(start, end]` on whole UTC days: the days from
    `start` to the newest ERA5 serves (`LAG` before `now`) for which `held` (the valid times
    held) lacks an hour, each run of missing days a window of at most `max_days`."""
    last = (now - LAG).astimezone(UTC).date()
    day = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    windows: list[tuple[datetime, datetime]] = []
    while day.date() <= last:
        complete = all(day + i * HOUR in held for i in range(24))
        if not complete:
            if windows and windows[-1][1] == day and (day - windows[-1][0]).days < max_days:
                windows[-1] = (windows[-1][0], day + timedelta(days=1))
            else:
                windows.append((day, day + timedelta(days=1)))
        day += timedelta(days=1)
    return [TimeRange(a - HOUR, b - HOUR) for a, b in windows]


def ingest(ctx: JobContext) -> None:
    """Live only, with a key: queue each due farm that has a pin."""
    if ctx.settings.cdsapi_key is None:
        return
    if not isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        return
    farms = ctx.session.scalars(select(Farm).order_by(Farm.id))
    farm_ids = [
        f.id
        for f in farms
        if farm_location(f) is not None and (ctx.farm_ids is None or f.id in ctx.farm_ids)
    ]
    tasks.enqueue_due(ctx.session, JOB_NAME, INTERVAL, farm_ids)


def _held(session: Session, farm_id: int, station: Station, since: datetime) -> set[datetime]:
    repo = repo_for_job(session, farm_id, JOB_NAME)
    held = repo.execute(
        select(WeatherObservation.period_end)
        .join(WeatherStation, WeatherStation.id == WeatherObservation.station_id)
        .where(
            WeatherStation.provider == provider.PROVIDER,
            WeatherStation.provider_station_id == station.provider_station_id,
            WeatherObservation.variable == VARIABLES["air_temperature"].name,
            WeatherObservation.period_end > since,
        ),
        scope=service.SCOPE,
        resource="weather_observation",
    ).scalars()
    return set(held)


def _pending(state: object, now: datetime) -> tuple[str, TimeRange] | None:
    """The order kept from an earlier run, unless it is older than `MAX_PENDING`."""
    try:
        order = state["pending"]  # type: ignore[index]
        window = TimeRange(
            datetime.fromisoformat(order["start"]), datetime.fromisoformat(order["end"])
        )
        if now - datetime.fromisoformat(order["submitted"]) > MAX_PENDING:
            return None
        return str(order["job"]), window
    except (TypeError, KeyError, ValueError):
        return None


def backfill_farm(ctx: tasks.TaskContext) -> None:
    """The durable task: order or collect outside any transaction, store only if the attempt is
    current. A window that fails never holds back the next; one still queued ends the run."""
    with session_scope(ctx.session_factory) as session:
        now = get_clock(session, ctx.settings).now()
        point = farm_location(session.get(Farm, ctx.farm_id))
        if point is None:
            return
        station = grid_station(Point(point["latitude"], point["longitude"]))
        start = season_start(now)
        held = _held(session, ctx.farm_id, station, start - HOUR)
        state = (session.get(Farm, ctx.farm_id).props or {}).get(HEALTH)
    era5 = provider.provider(ctx.settings, timeout=ctx.attempt.remaining() * FETCH_SHARE)
    kept = _pending(state, now)
    jobs: list[tuple[str | None, TimeRange]] = (
        [kept] if kept else [(None, w) for w in plan(held, now, start)[:MAX_WINDOWS]]
    )
    responses: list[Fetched] = []
    errors: list[str] = []
    pending = None
    for job, window in jobs:
        try:
            job = job or era5.submit(station, window)
            fetched = era5.collect(job, station, window)
        except provider.PortalError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:  # recorded, never raised into a retry loop with a stale plan
            errors.append(type(exc).__name__)
            continue
        if fetched is None:
            pending = {
                "job": job,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "submitted": state["pending"]["submitted"] if kept else now.isoformat(),
            }
            break
        responses.append(fetched)
    error = "; ".join(errors) or None
    if error is not None:
        logger.warning("%s farm %s: %s", JOB_NAME, ctx.farm_id, error)
    with session_scope(ctx.session_factory) as session:
        if not tasks.publish_if_current(session, ctx.attempt):
            return
        farm = session.get(Farm, ctx.farm_id, with_for_update=True)
        for fetched in responses:
            store(session, farm.id, era5, station, fetched)
        farm.props = {
            **(farm.props or {}),
            HEALTH: {
                "last_attempt": datetime.now(UTC).isoformat(),
                "error": error,
                "pending": pending,
            },
        }


def store(
    session: Session, farm_id: int, era5: provider.Era5, station: Station, fetched: Fetched
) -> None:
    """One response: its receipt (this provider, the farm) and observations under the grid
    point."""
    receipt = record_receipt(
        session,
        provider=era5.name,
        licence=era5.licence(),
        fetched=fetched,
        secret_fields=[f.name for f in era5.auth_schema() if f.secret],
        farm_id=farm_id,
    )
    if fetched.values:
        grid = upsert_station(session, provider.PROVIDER, station)
        store_observations(session, receipt, grid, list(fetched.values))
