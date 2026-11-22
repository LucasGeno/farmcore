"""`weather_era5.backfill`: the season's missing history from ERA5, for each farm with a pin.

Live mode only (replay never calls the network). With a CDS key set, the job queues each due
farm that has a location (`forecast_location`, the farm pin), once a day, and `farmcore worker`
runs `backfill_farm`:

1. read which hours the farm's ERA5 grid point already holds this season (no transaction stays
   open during the requests): an hour with a final value for every variable, or a provisional
   (ERA5T) one still too recent to be final (`provider.provisional_from`). Once old enough, a
   provisional hour is ordered again, and its final value stored beside it (`store`);
2. `plan` the whole days it lacks, from 1 January to the newest day the dataset serves (its
   public constraints), in windows of
   at most `MAX_DAYS`. A CDS order can queue for a long time, so a run orders one window and
   waits for it only until its deadline: an order still queued is kept in the farm's
   `era5_backfill` health and collected, not ordered again, on the next run. Windows are
   independent: a failed one never holds back the next;
3. only if the attempt is current: store each response under the farm's own grid-point station
   (`copernicus-era5`, `farm{id}:{lat},{lon}`, owned by the farm: private; a model, not a
   station: its receipt says `model-reanalysis`) and the
   health `{last_attempt, error, pending}`: each failed step and its HTTP status (never the
   key) and the order still waiting, if any.

Two farms in one 0.25 degree cell each have their own grid-point station, so each holds and
orders its own hours.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
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
from farmcore.core.weather.variables import BY_LEGACY
from farmcore.db import session_scope

logger = logging.getLogger(__name__)

JOB_NAME = "weather_era5.backfill"
INTERVAL = timedelta(days=1)
# One run: submit and poll for a few minutes; a longer queue is collected by a later run.
DEADLINE = timedelta(minutes=3)
HEALTH = "era5_backfill"

HOUR = timedelta(hours=1)
# Used only when the dataset's constraints cannot be read: this copy of ERA5 ended 11 days
# behind real time on 2026-11-19 (its catalogue says ERA5 is ~5 days behind), so 14 is assumed.
FALLBACK_LAG = timedelta(days=14)
# Assumed: a quarter keeps one order small (the CDS publishes no size limit for the dataset,
# and asks for small requests over large ones).
MAX_DAYS = 92
# A run orders at most this many windows; later ones wait for the next day's run.
MAX_WINDOWS = 3
# Assumed: an order not finished after this long is abandoned and ordered again. Well above
# INTERVAL: the next run starts more than INTERVAL after the last, so the order must outlive it.
MAX_PENDING = timedelta(days=5)
# The share of the attempt's deadline the requests may use: storing is fenced by the same
# deadline (`publish_if_current`), so the rest is left to write the receipts and the health.
FETCH_SHARE = 0.8
STORED = tuple(BY_LEGACY[n].name for n in ("rr1_mm", "t_c", "rh_pct", "ff_kmh"))  # per hour
GRID = 0.25  # degrees: the timeseries dataset's grid


def season_start(now: datetime) -> datetime:
    """Where the history begins: 1 January of the current year (hydrothermal time for crop
    models counts from it)."""
    return datetime(now.year, 1, 1, tzinfo=UTC)


def grid_station(point: Point, farm_id: int) -> Station:
    """The ERA5 grid point nearest `point`, as the farm's own station its hours are stored
    under (private to the farm: two farms in one cell never share one)."""
    # Half up (not Python's banker's rounding); a pin on a cell edge may still be put in the
    # neighbour by the CDS, whose row carries the real point.
    latitude = round(math.floor(point.latitude / GRID + 0.5) * GRID, 2)
    longitude = round(math.floor(point.longitude / GRID + 0.5) * GRID, 2)
    return Station(
        provider_station_id=f"farm{farm_id}:{latitude:.2f},{longitude:.2f}",
        name=f"ERA5 grid point {latitude:.2f}, {longitude:.2f} (model reanalysis)",
        latitude=latitude,
        longitude=longitude,
    )


def plan(
    held: Collection[datetime], last: date, start: datetime, max_days: int = MAX_DAYS
) -> list[TimeRange]:
    """The windows to order, oldest first, each `(start, end]` on whole UTC days: the days from
    `start` to `last`, the newest day the dataset serves, for which `held` (the valid times
    held) lacks an hour, each run of missing days a window of at most `max_days`."""
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


def _held(
    session: Session, farm_id: int, station: Station, since: datetime, now: datetime
) -> set[datetime]:
    repo = repo_for_job(session, farm_id, JOB_NAME)
    # A provisional value still too recent to be final counts as held: ordering it again would
    # bring the same ERA5T.
    settled = or_(
        WeatherObservation.provisional.is_(False),
        WeatherObservation.period_end >= provider.provisional_from(now),
    )
    held = repo.execute(
        select(WeatherObservation.period_end)
        .join(WeatherStation, WeatherStation.id == WeatherObservation.station_id)
        .where(
            WeatherStation.provider == provider.PROVIDER,
            WeatherStation.provider_station_id == station.provider_station_id,
            WeatherObservation.period_end > since,
        )
        .group_by(WeatherObservation.period_end)
        # An hour is held only with every variable (a member missing from a response must not
        # leave its variable unfilled for good).
        .having(func.count(WeatherObservation.variable.distinct()).filter(settled) == len(STORED)),
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
        station = grid_station(Point(point["latitude"], point["longitude"]), ctx.farm_id)
        start = season_start(now)
        held = _held(session, ctx.farm_id, station, start - HOUR, now)
        state = (session.get(Farm, ctx.farm_id).props or {}).get(HEALTH)
    era5 = provider.provider(ctx.settings, timeout=ctx.attempt.remaining() * FETCH_SHARE)
    responses: list[Fetched] = []
    errors: list[str] = []
    pending = None
    kept = _pending(state, now)
    jobs: list[tuple[str | None, TimeRange]] = [kept] if kept else []
    if not kept:
        try:
            last = era5.last_day()
        except Exception as exc:  # the newest day is then guessed, never a reason to skip a run
            errors.append(str(exc) if isinstance(exc, provider.PortalError) else type(exc).__name__)
            last = (now - FALLBACK_LAG).date()
        jobs = [(None, w) for w in plan(held, last, start)[:MAX_WINDOWS]]
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
    """One response: its receipt (this provider, the farm) and observations under the farm's
    grid point."""
    receipt = record_receipt(
        session,
        provider=era5.name,
        licence=era5.licence(fetched.fetched_at),
        fetched=fetched,
        secret_fields=[f.name for f in era5.auth_schema() if f.secret],
        farm_id=farm_id,
        tier=era5.capabilities().tier,
    )
    if fetched.values:
        grid = upsert_station(session, provider.PROVIDER, station, owner_farm_id=farm_id)
        store_observations(session, receipt, grid, list(fetched.values))
