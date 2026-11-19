"""`stations.ingest_meteofrance`: replay ingest of the demo station, with connector health.

Each run stores the CSV rows with `ts <= ctx.now + 73 h` (`INGEST_HORIZON`: the 72 h replay
forecast plus the hour the advisor window's rain lookahead reads past it) that are newer than the
latest observation the farm holds in the normalised schema (so a re-run adds nothing, a later step
adds only newer hours, and an installation that held only legacy rows fills the normalised schema
on its next run), through `provider.store_hours` (legacy rows and the normalised schema). It then
writes the health into the station's `props`: `last_ts`, `rows` (stored observations),
`nulls_skipped` (null cells up to `last_ts`), `last_run` (clock time) and `file`. The health counts
only rows with `ts <= ctx.now + 73 h`, so stepping the clock back never shows hours stored by a
later step.

A missing CSV, or a `file` prop that resolves outside `data_dir`, records
`{last_run, error: "file missing"}` and returns; a good run clears `error`.

In live mode the job only queues the demo farm when due (`core/tasks.py`) and the worker runs
`ingest_live`, which stores nothing unless its attempt is still current.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from farmcore.apps.stations import connector, provider, service
from farmcore.core import tasks
from farmcore.core.access import FarmNotSeeded, default_farm, repo_for_job
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Observation
from farmcore.db import session_scope
from farmcore.settings import Settings

JOB_NAME = "stations.ingest_meteofrance"
INTERVAL = timedelta(hours=1)
logger = logging.getLogger(__name__)
FILE_MISSING = "file missing"
# The advisor's window reads rain after its last horizon hour: `forecast_span` = 72 h horizon
# + 2 h lookahead − 1 h = 73 h with the defaults (stations never imports advisor; the contract
# test tests/apps/advisor/test_forecast_span.py checks this covers every season config).
INGEST_HORIZON = connector.REPLAY_HORIZON + timedelta(hours=1)


def _csv_path(data_dir: Path, file: str) -> Path | None:
    """`data_dir / file` if it is an existing file inside `data_dir`, else None."""
    root = data_dir.resolve()
    path = (root / file).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def ingest_meteofrance(ctx: JobContext) -> None:
    """Replay: ingest at `ctx.now`, in this transaction. Live: only queue the demo farm when due;
    `farmcore worker` runs `ingest_live` for it."""
    try:
        farm = default_farm(ctx.session, ctx.settings)
    except FarmNotSeeded:
        logger.info("%s skipped: no demo farm (run `farmcore seed`)", JOB_NAME)
        return
    if ctx.farm_ids is not None and farm.id not in ctx.farm_ids:
        return
    if isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        tasks.enqueue_due(ctx.session, JOB_NAME, INTERVAL, [farm.id])
        return
    _ingest(ctx.session, ctx.settings, farm.id, ctx.now)


def ingest_live(ctx: tasks.TaskContext) -> None:
    """The durable task: ingest at the live now, written only if the attempt is current."""
    with session_scope(ctx.session_factory) as session:
        if tasks.publish_if_current(session, ctx.attempt):
            _ingest(session, ctx.settings, ctx.farm_id, get_clock(session, ctx.settings).now())


def _ingest(session: Session, settings: Settings, farm_id: int, now: datetime) -> None:
    repo = repo_for_job(session, farm_id, JOB_NAME)
    station = service.station_for_farm(repo)
    if station is None:
        logger.info("%s skipped: no station seeded (run `farmcore seed`)", JOB_NAME)
        return

    until = now + INGEST_HORIZON

    def stored(agg: Any, *, bounded: bool = True) -> Any:
        """One aggregate over this station's stored observations (up to `until` if bounded)."""
        stmt = select(agg).where(Observation.asset_id == station.id)
        if bounded:
            stmt = stmt.where(Observation.ts <= until)
        return repo.execute(stmt, scope=service.SCOPE, resource="observation").scalar()

    file = station.props.get("file", connector.FILE)
    path = _csv_path(settings.data_dir, file)
    if path is None:
        logger.warning("%s: %s (%s)", JOB_NAME, FILE_MISSING, file)
        station.props = {**station.props, "last_run": now.isoformat(), "error": FILE_MISSING}
        session.flush()
        return
    hours = connector.read_hours(path)
    latest = service.latest_reading(repo, station.id)
    new = [h for h in hours if h.ts <= until and (latest is None or h.ts > latest)]
    provider.store_hours(session, station, new, now)

    last_ts = stored(func.max(Observation.ts))
    nulls = sum(
        x is None
        for h in hours
        if last_ts is not None and h.ts <= last_ts
        for x in h.values.values()
    )
    props = {k: v for k, v in station.props.items() if k != "error"}
    station.props = {
        **props,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "rows": stored(func.count()),
        "nulls_skipped": nulls,
        "last_run": now.isoformat(),
        "file": file,
    }
    session.flush()
