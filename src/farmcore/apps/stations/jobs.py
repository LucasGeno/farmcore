"""`stations.ingest_meteofrance`: replay ingest of the demo station, with connector health.

Each run stores the CSV rows with `ts <= ctx.now + 73 h` (`INGEST_HORIZON`: the 72 h replay
forecast plus the hour the advisor window's rain lookahead reads past it) that are newer than the
latest stored observation (so a re-run adds nothing and a later step adds only newer hours), then
writes the health into the station's `props`: `last_ts`, `rows` (stored observations),
`nulls_skipped` (null cells up to `last_ts`), `last_run` (clock time) and `file`. The health counts
only rows with `ts <= ctx.now + 73 h`, so stepping the clock back never shows hours stored by a
later step.

A missing CSV, or a `file` prop that resolves outside `data_dir`, records
`{last_run, error: "file missing"}` and returns; a good run clears `error`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from farmcore.apps.stations import connector, service
from farmcore.core.access import FarmNotSeeded, default_farm, repo_for_job
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Observation

JOB_NAME = "stations.ingest_meteofrance"
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
    try:
        farm = default_farm(ctx.session, ctx.settings)
    except FarmNotSeeded:
        logger.info("%s skipped: no demo farm (run `farmcore seed`)", JOB_NAME)
        return
    if ctx.farm_ids is not None and farm.id not in ctx.farm_ids:
        return
    repo = repo_for_job(ctx.session, farm.id, JOB_NAME)
    station = service.station_for_farm(repo)
    if station is None:
        logger.info("%s skipped: no station seeded (run `farmcore seed`)", JOB_NAME)
        return

    until = ctx.now + INGEST_HORIZON

    def stored(agg: Any, *, bounded: bool = True) -> Any:
        """One aggregate over this station's stored observations (up to `until` if bounded)."""
        stmt = select(agg).where(Observation.asset_id == station.id)
        if bounded:
            stmt = stmt.where(Observation.ts <= until)
        return repo.execute(stmt, scope=service.SCOPE, resource="observation").scalar()

    file = station.props.get("file", connector.FILE)
    path = _csv_path(ctx.settings.data_dir, file)
    if path is None:
        logger.warning("%s: %s (%s)", JOB_NAME, FILE_MISSING, file)
        station.props = {**station.props, "last_run": ctx.now.isoformat(), "error": FILE_MISSING}
        ctx.session.flush()
        return
    hours = connector.read_hours(path)
    latest = stored(func.max(Observation.ts), bounded=False)
    new = [h for h in hours if h.ts <= until and (latest is None or h.ts > latest)]
    connector.upsert(ctx.session, station.id, connector.observations(new))

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
        "last_run": ctx.now.isoformat(),
        "file": file,
    }
    ctx.session.flush()
