"""Durable background tasks for live work, and the fence that keeps a stale attempt from publishing.

A manifest job with a `task` (`JobSpec.task`) is durable: in live mode its job function plans,
enqueueing one task per farm in the caller's transaction (`enqueue`), and `farmcore worker`
processes run those tasks. Replay never enqueues; `step_to` stays in-process and deterministic.

The queue is Procrastinate (MIT), and this is the only module that imports it, so swapping it for
a plain `SKIP LOCKED` table changes nothing else. FarmCore does not trust the library's job status
to decide what may be written: each run takes a new attempt in `task_attempt` (`begin_attempt`),
and the transaction that writes its result first checks, under row locks, that this attempt is
still the current one for its task and farm, that the farm still exists, that its deadline has
not passed and that the clock is still live in the generation the attempt started in
(`publish_if_current`). A retried task, a live/replay switch and a restore therefore fence every
older attempt without listing them, whatever the library believes about its job.

Library gaps closed here: enqueueing inside a caller's transaction uses a synchronous job manager
on that transaction's own connection, in a savepoint (a duplicate aborts only the savepoint);
the worker retries the tasks of workers whose heartbeat stopped (`retry_stalled`), failing a
stalled task whose successor is already queued (upstream issue #1446) or that used up its
attempts; there is no per-task timeout (#1281), so a task bounds its own I/O by its attempt's
deadline and the fence rejects anything later.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import procrastinate
from procrastinate import exceptions as queue_errors
from procrastinate.jobs import Status
from procrastinate.manager import JobManager
from procrastinate.tasks import configure_task
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext, JobSpec, Manifest, import_string
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

QUEUE = "farmcore"
# Runs of one task, the first included, before a crashed or stalled task is failed for good.
MAX_ATTEMPTS = 3
# Planning ticks and stalled-task checks, per worker.
TICK_SECONDS = 15.0
# A worker whose heartbeat is older than this is presumed dead and its tasks are retried. With
# the tick, a dead worker's task is retried (20 + 15 s) before its attempt's deadline (one
# minute by default) lets planning queue the farm again: one fetch per crash, not two.
STALLED_SECONDS = 20.0
HEARTBEAT_SECONDS = 5.0
# Enqueue only: with `connection=` the manager never opens a pool of its own.
_ENQUEUE = JobManager(procrastinate.SyncPsycopgConnector())


@dataclass(frozen=True)
class Attempt:
    task_name: str
    farm_id: int
    number: int
    generation: int
    deadline_at: datetime

    def remaining(self) -> float:
        """Seconds left before the deadline (0 when past): the budget for any I/O."""
        return max(0.0, (self.deadline_at - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True)
class TaskContext:
    """What a durable task function (`JobSpec.task`) receives. It opens its own short
    transactions, never holds one across network I/O, and writes its result only in a
    transaction where `publish_if_current(session, attempt)` returned True."""

    session_factory: sessionmaker[Session]
    settings: Settings
    farm_id: int
    attempt: Attempt


def durable_specs(manifests: Sequence[Manifest]) -> list[JobSpec]:
    return [spec for m in manifests for spec in m.jobs if spec.task is not None]


def queueing_lock(task_name: str, farm_id: int) -> str:
    return f"{task_name}:farm={farm_id}"


def enqueue(session: Session, task_name: str, farm_id: int) -> bool:
    """Queue `task_name` for `farm_id` in the session's transaction: it exists only if that
    commits. False when the same task for that farm is already queued."""
    try:
        with session.begin_nested():
            # Inside the savepoint (asking for the connection emits it): a duplicate aborts only
            # the savepoint, never the caller's transaction.
            configure_task(
                name=task_name,
                job_manager=_ENQUEUE,
                queue=QUEUE,
                queueing_lock=queueing_lock(task_name, farm_id),
                task_kwargs={"farm_id": farm_id},
                connection=session.connection().connection.driver_connection,
            ).defer()
    except queue_errors.AlreadyEnqueued:
        return False
    return True


def in_flight(session: Session, task_name: str) -> set[int]:
    """Farms with an unfinished attempt of `task_name` still before its deadline, in the current
    clock generation: planning skips them, so a running task is never superseded early."""
    return set(
        session.execute(
            text(
                "SELECT a.farm_id FROM task_attempt a JOIN clock_state c "
                "ON c.id = 1 AND c.generation = a.clock_generation "
                "WHERE a.task_name = :n AND a.finished_at IS NULL AND a.deadline_at > now()"
            ),
            {"n": task_name},
        ).scalars()
    )


def begin_attempt(
    session_factory: sessionmaker[Session], task_name: str, farm_id: int, deadline: timedelta
) -> Attempt | None:
    """Take the next attempt of `task_name` for `farm_id`, in its own transaction; any older
    attempt can no longer publish. None while the clock is not live or the farm is gone."""
    with session_scope(session_factory) as session:
        row = session.execute(
            text(
                "INSERT INTO task_attempt AS a (task_name, farm_id, attempt, clock_generation, "
                "started_at, deadline_at) "
                "SELECT :n, f.id, 1, c.generation, now(), now() + :d FROM farm f "
                "JOIN clock_state c ON c.id = 1 AND c.mode = 'live' WHERE f.id = :f "
                "ON CONFLICT (task_name, farm_id) DO UPDATE SET attempt = a.attempt + 1, "
                "clock_generation = excluded.clock_generation, started_at = excluded.started_at, "
                "deadline_at = excluded.deadline_at, finished_at = NULL "
                "RETURNING attempt, clock_generation, deadline_at"
            ),
            {"n": task_name, "f": farm_id, "d": deadline},
        ).first()
    if row is None:
        return None
    return Attempt(task_name, farm_id, row.attempt, row.clock_generation, row.deadline_at)


def publish_if_current(session: Session, attempt: Attempt) -> bool:
    """The first statement of the transaction that writes `attempt`'s result. True when the
    attempt may publish: it is still current and before its deadline, its farm exists, and the
    clock is live in the attempt's generation. The rows stay locked until that transaction ends,
    so a newer attempt or a clock switch waits for it rather than interleaving. True also marks
    the attempt finished (in the same transaction). On False the caller writes nothing."""
    current = session.execute(
        text(
            "SELECT 1 FROM task_attempt a "
            "JOIN clock_state c ON c.id = 1 AND c.mode = 'live' "
            "AND c.generation = a.clock_generation "
            "JOIN farm f ON f.id = a.farm_id "
            "WHERE a.task_name = :n AND a.farm_id = :f AND a.attempt = :a "
            "AND a.finished_at IS NULL AND a.deadline_at > now() "
            "FOR UPDATE OF a FOR SHARE OF c FOR KEY SHARE OF f"
        ),
        {"n": attempt.task_name, "f": attempt.farm_id, "a": attempt.number},
    ).first()
    if current is None:
        logger.warning(
            "stale attempt %s of %s for farm %s: not published",
            attempt.number,
            attempt.task_name,
            attempt.farm_id,
        )
        return False
    session.execute(
        text(
            "UPDATE task_attempt SET finished_at = now() "
            "WHERE task_name = :n AND farm_id = :f AND attempt = :a"
        ),
        {"n": attempt.task_name, "f": attempt.farm_id, "a": attempt.number},
    )
    return True


def run_task(
    session_factory: sessionmaker[Session], settings: Settings, spec: JobSpec, farm_id: int
) -> None:
    """One run of `spec.task` for `farm_id`: what a worker does with a queued task."""
    attempt = begin_attempt(session_factory, spec.name, farm_id, spec.deadline)
    if attempt is None:
        logger.info("task %s for farm %s skipped: clock not live or farm gone", spec.name, farm_id)
        return
    import_string(spec.task)(TaskContext(session_factory, settings, farm_id, attempt))


def plan(
    session_factory: sessionmaker[Session], settings: Settings, manifests: Sequence[Manifest]
) -> None:
    """Run each durable job function once, in live mode only: each enqueues its due work."""
    for spec in durable_specs(manifests):
        try:
            with session_scope(session_factory) as session:
                clock = get_clock(session, settings)
                if not isinstance(clock, LiveClock):
                    return
                import_string(spec.func)(JobContext(session, clock.now(), settings))
        except Exception as exc:  # one failing planner must not stop the others or the worker
            logger.error("planning %s failed (%s)", spec.name, type(exc).__name__)


async def retry_stalled(job_manager: JobManager, seconds_since_heartbeat: float) -> dict[int, str]:
    """Requeue the tasks of workers whose heartbeat is older than `seconds_since_heartbeat`.
    A task that used up `MAX_ATTEMPTS`, or whose successor for the same farm is already queued
    (the library's retry then violates the queueing lock: upstream #1446), is failed instead.
    Returns {job id: "retried" | "failed"}."""
    outcomes: dict[int, str] = {}
    for job in await job_manager.get_stalled_jobs(seconds_since_heartbeat=seconds_since_heartbeat):
        assert job.id is not None
        retry = job.attempts + 1 < MAX_ATTEMPTS
        try:
            if retry:
                try:
                    await job_manager.retry_job(job)
                    outcomes[job.id] = "retried"
                    continue
                except queue_errors.UniqueViolation:
                    pass
            await job_manager.finish_job(job, Status.FAILED, delete_job=False)
            outcomes[job.id] = "failed"
        except queue_errors.ConnectorException:
            pass  # another worker moved it first
    return outcomes


def conninfo(settings: Settings) -> str:
    """libpq connection string of `settings.database_url` (an SQLAlchemy URL)."""
    url = make_url(settings.database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def make_app(
    settings: Settings, session_factory: sessionmaker[Session], manifests: Sequence[Manifest]
) -> procrastinate.App:
    """A queue app whose tasks are the durable jobs of `manifests`."""
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo(settings)))
    for spec in durable_specs(manifests):

        def run(farm_id: int, spec: JobSpec = spec) -> None:
            run_task(session_factory, settings, spec, farm_id)

        app.task(name=spec.name, queue=QUEUE)(run)
    return app


async def _housekeeping(
    app: procrastinate.App,
    session_factory: sessionmaker[Session],
    settings: Settings,
    manifests: Sequence[Manifest],
    tick: float,
    stalled: float,
) -> None:
    while True:
        await asyncio.to_thread(plan, session_factory, settings, manifests)
        try:
            for job_id, outcome in (await retry_stalled(app.job_manager, stalled)).items():
                logger.warning("stalled task %s %s", job_id, outcome)
        except queue_errors.ConnectorException as exc:
            logger.error("stalled-task check failed (%s)", type(exc).__name__)
        await asyncio.sleep(tick)


def run_worker(
    settings: Settings,
    session_factory: sessionmaker[Session],
    manifests: Sequence[Manifest],
    *,
    concurrency: int,
    tick: float = TICK_SECONDS,
    stalled: float = STALLED_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> None:
    """Run tasks until SIGTERM or SIGINT, planning due work and retrying stalled tasks every
    `tick` seconds. `concurrency` tasks run at once: more than the farms expected to be slow at
    the same time, so one farm's slow provider never holds up another farm's task."""
    app = make_app(settings, session_factory, manifests)

    async def main() -> None:
        async with app.open_async():
            housekeeping = asyncio.create_task(
                _housekeeping(app, session_factory, settings, manifests, tick, stalled)
            )
            try:
                await app.run_worker_async(
                    queues=[QUEUE],
                    concurrency=concurrency,
                    update_heartbeat_interval=heartbeat,
                    stalled_worker_timeout=stalled,
                    install_signal_handlers=True,
                )
            finally:
                housekeeping.cancel()

    logger.info("worker %s: %d task slot(s)", os.getpid(), concurrency)
    asyncio.run(main())
