"""Write imported runs; read the run a clock time may see.

`import_runs` (the CLI) inserts each run once (key: farm, source, issue time) with its values.
`latest_run(repo, now)` is the only read: the latest run with `available_at <= now` (the run
users could read by then), as advisor `HourlyObs` rows, so a replay at 9 Apr 06:00 never sees a
run that was not out yet; a run whose last hour is before `now` counts as none.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.advisor.engine import HourlyObs
from farmcore.apps.forecast.models import ForecastRun, ForecastValue
from farmcore.apps.forecast.sources import Run
from farmcore.core.access import ScopedRepository

SCOPE = "stations:read"
GUSTS = "fx_kmh"  # not an `HourlyObs` field: no advisor rule reads gusts


@dataclass(frozen=True)
class StoredRun:
    source: str
    issued_at: datetime
    available_at: datetime
    label: str
    as_issued: bool
    series: list[HourlyObs]
    gusts: dict[datetime, float] = field(default_factory=dict)  # information only
    props: dict[str, Any] = field(default_factory=dict)


def import_runs(session: Session, farm_id: int, runs: Iterable[Run]) -> dict[str, int]:
    """Insert the runs the farm does not hold yet; returns the counts written."""
    counts = {"runs": 0, "values": 0}
    for run in runs:
        held = session.scalar(
            select(ForecastRun.id).where(
                ForecastRun.farm_id == farm_id,
                ForecastRun.source == run.source,
                ForecastRun.issued_at == run.issued_at,
            )
        )
        if held is not None:
            continue
        row = ForecastRun(
            farm_id=farm_id,
            source=run.source,
            issued_at=run.issued_at,
            available_at=run.available,
            props={
                "label": run.label,
                "as_issued": run.as_issued,
                "latitude": run.latitude,
                "longitude": run.longitude,
                **run.props,
            },
        )
        session.add(row)
        session.flush()
        session.add_all(
            ForecastValue(farm_id=farm_id, run_id=row.id, ts=ts, variable=variable, value=value)
            for ts, variable, value in run.values
        )
        session.flush()
        counts["runs"] += 1
        counts["values"] += len(run.values)
    return counts


def latest_run(repo: ScopedRepository, now: datetime) -> StoredRun | None:
    runs = repo.list(
        ForecastRun,
        ForecastRun.available_at <= now,
        scope=SCOPE,
        order_by=(
            ForecastRun.available_at.desc(),
            ForecastRun.issued_at.desc(),
            ForecastRun.id.desc(),
        ),
        limit=1,
    )
    if not runs:
        return None
    run = runs[0]
    last = repo.list(
        ForecastValue,
        ForecastValue.run_id == run.id,
        scope=SCOPE,
        order_by=ForecastValue.ts.desc(),
        limit=1,
    )
    if not last or last[0].ts < now:
        return None  # a stale run (the last import ended before now) is no forecast
    by_ts: dict[datetime, dict[str, float]] = defaultdict(dict)
    gusts: dict[datetime, float] = {}
    for v in repo.list(ForecastValue, ForecastValue.run_id == run.id, scope=SCOPE):
        if v.variable == GUSTS:
            gusts[v.ts] = v.value
        else:
            by_ts[v.ts][v.variable] = v.value
    props = run.props or {}
    return StoredRun(
        source=run.source,
        issued_at=run.issued_at,
        available_at=run.available_at,
        label=props.get("label") or run.source,
        as_issued=bool(props.get("as_issued")),
        series=[HourlyObs(ts=ts, **fields) for ts, fields in sorted(by_ts.items())],
        gusts=gusts,
        props=props,
    )
