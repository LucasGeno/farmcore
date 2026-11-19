"""Manifest job runner: replay `step_to`, `run_now`, and the live jobs `farmcore worker` runs."""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core import registry, tasks
from farmcore.core.clock import LiveClock, clock_row, get_clock, set_replay
from farmcore.core.manifest import JobContext, JobSpec, Manifest, import_string
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

# `clock_state.job_runs` key: when each installation-wide job last ran live.
LIVE_LAST_RUN = "live_last_run"
# `JobRun.error` of a live `run_now` whose queued task did not publish within its wait.
QUEUED = "queued"

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
        manifests = registry.live_active(session, manifests)  # an app off runs no job
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
    *,
    farm_ids: tuple[int, ...] | None = None,
    wait: float = 0.0,
) -> JobRun | None:
    """Run job `name` once at the current clock; None when no app that is on declares it.

    In live mode a durable job (`JobSpec.task`) for given farms is queued for `farmcore worker`
    instead, never run here: the call then waits at most `wait` seconds for an evaluation that
    began after it to publish for every farm, and is not ok ("queued") if none did in time."""
    with session_factory() as session:
        manifests = registry.live_active(session, manifests)
    spec = next((s for m in manifests for s in m.jobs if s.name == name), None)
    if spec is None:
        return None
    if spec.task is not None and farm_ids:
        started = time.perf_counter()
        with session_scope(session_factory) as session:
            live = isinstance(get_clock(session, settings), LiveClock)
            if live:
                since = session.scalar(text("SELECT now()"))
                for farm_id in farm_ids:
                    tasks.enqueue(session, spec.name, farm_id)
        if live:
            ok = tasks.wait_published(session_factory, spec.name, farm_ids, since, wait)
            ms = round((time.perf_counter() - started) * 1000)
            return JobRun(name=spec.name, ok=ok, error=None if ok else QUEUED, ms=ms)
    with session_scope(session_factory) as coordinator:
        coordinator.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STEP_LOCK_KEY})
        with session_factory() as session:
            now = get_clock(session, settings).now()
        return _execute(session_factory, spec, now, settings, farm_ids=farm_ids)


def run_due_live(
    session_factory: sessionmaker[Session], manifests: Sequence[Manifest], settings: Settings
) -> list[JobRun]:
    """What `farmcore worker` does each tick for jobs without a durable task: in live mode, run
    each one of an app that is on now whose interval has passed since its last live run, in
    manifest order, at the wall time. The step lock is held throughout, so the mode cannot change
    under a job and two workers never both run one; the schedule is kept in `clock_state`, so a
    restart forgets nothing. A job is first due one interval after a worker first sees it (the
    replay schedule is separate). Only a successful run moves its schedule on, as in
    `step_to`."""
    with session_scope(session_factory) as coordinator:
        coordinator.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STEP_LOCK_KEY})
        with session_scope(session_factory) as session:
            clock = get_clock(session, settings)
            if not isinstance(clock, LiveClock):
                return []
            now = clock.now()
            last_runs = dict(clock_row(session, settings).job_runs.get(LIVE_LAST_RUN, {}))
            manifests = registry.live_active(session, manifests)  # an app off runs no job
        runs = []
        for spec in (s for m in manifests for s in m.jobs if s.task is None):
            prev = last_runs.get(spec.name)
            if prev is None:
                last_runs[spec.name] = now.isoformat()
                continue
            if not _is_due(spec, datetime.fromisoformat(prev), now):
                continue
            run = _execute(session_factory, spec, now, settings)
            runs.append(run)
            if run.ok:
                last_runs[spec.name] = now.isoformat()
        with session_scope(session_factory) as session:
            row = clock_row(session, settings)
            row.job_runs = {**row.job_runs, LIVE_LAST_RUN: last_runs}
        return runs
