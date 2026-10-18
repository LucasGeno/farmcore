"""Manifest job runner: replay `step_to`, `run_now`, live APScheduler."""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core.clock import LiveClock, clock_row, get_clock, set_replay
from farmcore.core.manifest import JobContext, JobSpec, Manifest, import_string
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

# pg_advisory_xact_lock key serialising `step_to` across processes (web, bot, CLI): "farm".
STEP_LOCK_KEY = 0x6661726D


@dataclass(frozen=True)
class JobRun:
    name: str
    ok: bool
    error: str | None
    ms: int


def _execute(
    session_factory: sessionmaker[Session],
    spec: JobSpec,
    now: datetime,
    settings: Settings,
    *,
    farm_ids: tuple[int, ...] | None = None,
) -> JobRun:
    started = time.perf_counter()
    error = None
    try:
        func = import_string(spec.func)
        with session_scope(session_factory) as session:
            func(JobContext(session=session, now=now, settings=settings, farm_ids=farm_ids))
    except Exception as exc:
        # Exception messages may quote data rows (raw data never reaches a log, stdout,
        # clock_state or the navbar): record the type only, log the type plus frame locations.
        error = type(exc).__name__
        frames = "\n".join(
            f'  File "{f.filename}", line {f.lineno}, in {f.name}'
            for f in traceback.extract_tb(exc.__traceback__)
        )
        logger.error("job %s failed (%s)\n%s", spec.name, error, frames)
    ms = round((time.perf_counter() - started) * 1000)
    return JobRun(name=spec.name, ok=error is None, error=error, ms=ms)


def _is_due(spec: JobSpec, last_run: datetime | None, t: datetime) -> bool:
    return last_run is None or t < last_run or last_run + spec.interval <= t


def step_to(
    session_factory: sessionmaker[Session],
    manifests: Sequence[Manifest],
    t: datetime,
    settings: Settings,
) -> list[JobRun]:
    """Set replay now to `t` (aware), then run each due job once, in manifest order.

    Due: never run, `last_run + interval <= t`, or `t < last_run` (stepping back resets).
    Each job runs in its own transaction; a failure is recorded and later jobs still run.
    Only successful runs advance `last_run`, so a failed job is retried on the next step.
    Steps are serialised (a transaction-level advisory lock held for the whole step), so an
    overlapping step waits and then sees this step's `last_run`.
    """
    with session_scope(session_factory) as lock:
        lock.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STEP_LOCK_KEY})
        return step_locked(session_factory, manifests, t, settings)


def step_locked(
    session_factory: sessionmaker[Session],
    manifests: Sequence[Manifest],
    t: datetime,
    settings: Settings,
    *,
    farm_ids: tuple[int, ...] | None = None,
    invalidate_preview: bool = True,
) -> list[JobRun]:
    """Caller holds STEP_LOCK_KEY; scoped runs keep separate due-job metadata."""
    scope_key = ",".join(map(str, sorted(farm_ids))) if farm_ids is not None else None
    with session_scope(session_factory) as session:
        set_replay(session, t, settings)  # rejects a naive t
        if invalidate_preview:
            row = clock_row(session, settings)
            meta = dict(row.job_runs.get("demo") or {})
            row.job_runs = {
                **row.job_runs,
                "demo": {
                    **meta,
                    "generation": meta.get("generation", 0) + 1,
                },
            }
        t = t.astimezone(UTC)
        state = clock_row(session, settings).job_runs
        farm_runs = {key: dict(runs) for key, runs in state.get("farm_last_run", {}).items()}
        last_runs = dict(
            state.get("farm_last_run", {}).get(scope_key, {})
            if scope_key is not None
            else state.get("last_run", {})
        )

    runs = []
    for manifest in manifests:
        for spec in manifest.jobs:
            prev = last_runs.get(spec.name)
            due = _is_due(spec, datetime.fromisoformat(prev) if prev else None, t)
            catch_up = []
            if scope_key is None and not due:
                # A demo reset can rewind one farm while the global schedule stays ahead.
                # Catch up only those scoped schedules; other farms already ran at this time.
                for key, schedule in farm_runs.items():
                    previous = schedule.get(spec.name)
                    if _is_due(spec, datetime.fromisoformat(previous) if previous else None, t):
                        catch_up.append(key)
            if not due and not catch_up:
                continue
            targets = (
                tuple(sorted({int(fid) for key in catch_up for fid in key.split(",")}))
                if catch_up
                else farm_ids
            )
            run = _execute(session_factory, spec, t, settings, farm_ids=targets)
            runs.append(run)
            if run.ok:
                if due:
                    last_runs[spec.name] = t.isoformat()
                if scope_key is None:
                    # A successful full run covers every scope; a catch-up covers only its farms.
                    for key in catch_up if catch_up else farm_runs:
                        farm_runs[key][spec.name] = t.isoformat()

    with session_scope(session_factory) as session:
        row = clock_row(session, settings)
        schedule = (
            {"farm_last_run": {**row.job_runs.get("farm_last_run", {}), scope_key: last_runs}}
            if scope_key is not None
            else {"last_run": last_runs, "farm_last_run": farm_runs}
        )
        row.job_runs = {
            **row.job_runs,
            **schedule,
            "last_step": {"to": t.isoformat(), "runs": [asdict(r) for r in runs]},
        }
    return runs


def run_now(
    session_factory: sessionmaker[Session],
    manifests: Sequence[Manifest],
    name: str,
    settings: Settings,
) -> JobRun | None:
    """Run job `name` once at the current clock; None when no manifest declares it."""
    spec = next((s for m in manifests for s in m.jobs if s.name == name), None)
    if spec is None:
        return None
    with session_scope(session_factory) as coordinator:
        coordinator.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STEP_LOCK_KEY})
        with session_factory() as session:
            now = get_clock(session, settings).now()
        return _execute(session_factory, spec, now, settings)


def _run_live(session_factory: sessionmaker[Session], spec: JobSpec, settings: Settings) -> None:
    """One live tick. Re-reads the shared mode: after a step (web or CLI) switched clock_state
    to replay, ticks skip until it is live again, so jobs never run at a different "now"."""
    with session_scope(session_factory) as coordinator:
        coordinator.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STEP_LOCK_KEY})
        with session_factory() as session:
            clock = get_clock(session, settings)
        if not isinstance(clock, LiveClock):
            logger.debug("job %s skipped: clock is in replay mode", spec.name)
            return
        _execute(session_factory, spec, clock.now(), settings)


def start(app: FastAPI) -> None:
    """Live mode (per `clock_state`): one APScheduler interval job per JobSpec, first run one
    interval after start; each tick skips while clock_state is replay. Replay mode schedules
    nothing; `step_to` drives the jobs."""
    settings: Settings = app.state.settings
    session_factory: sessionmaker[Session] = app.state.session_factory
    app.state.scheduler = None
    with session_factory() as session:
        live = isinstance(get_clock(session, settings), LiveClock)
    if not live:
        return
    scheduler = BackgroundScheduler(timezone=UTC)
    for manifest in app.state.manifests:
        for spec in manifest.jobs:
            scheduler.add_job(
                _run_live,
                "interval",
                seconds=spec.interval.total_seconds(),
                id=spec.name,
                name=spec.name,
                args=(session_factory, spec, settings),
                max_instances=1,
                coalesce=True,
            )
    scheduler.start()
    app.state.scheduler = scheduler


def shutdown(app: FastAPI) -> None:
    scheduler: BackgroundScheduler | None = getattr(app.state, "scheduler", None)
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
