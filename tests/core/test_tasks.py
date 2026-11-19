"""Durable tasks: the publication fence, transactional enqueue and stalled-task retry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import procrastinate
import pytest
from sqlalchemy import text

from farmcore.core import clock, tasks
from farmcore.core.manifest import JobSpec
from farmcore.core.models.canonical import Farm
from farmcore.core.models.kernel import AppModule

TASK = "test.fetch"
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
ZERO = timedelta(0)


def _jobs(session) -> list[tuple]:
    return session.execute(
        text("SELECT task_name, status::text, queueing_lock FROM procrastinate_jobs ORDER BY id")
    ).all()


def _supersede(factory, settings, farm_id) -> None:
    tasks.begin_attempt(factory, TASK, farm_id, MINUTE)


def _switch_to_replay_and_back(factory, settings, farm_id) -> None:
    with factory() as session:
        clock.set_replay(session, datetime(2024, 4, 10, tzinfo=UTC), settings)
        clock.set_live(session, settings)
        session.commit()


def _module_state(factory, *states: str) -> None:
    """Switch TASK's module (`test`) through `states`, each committed, as `/apps` would."""
    for state in states:
        with factory() as session:
            row = session.get(AppModule, "test")
            if row is None:
                session.add(AppModule(name="test", version="0", state=state))
            else:
                row.state = state
            session.commit()


def _disable_module(factory, settings, farm_id) -> None:
    _module_state(factory, "uninstalled")


def _disable_and_reenable_module(factory, settings, farm_id) -> None:
    _module_state(factory, "uninstalled", "installed")


def _delete_farm(factory, settings, farm_id) -> None:
    with factory() as session:
        session.delete(session.get(Farm, farm_id))
        session.commit()


@pytest.mark.parametrize(
    ("change", "deadline", "publishes"),
    [
        (None, MINUTE, True),
        (_supersede, MINUTE, False),  # a retry took over
        (_switch_to_replay_and_back, MINUTE, False),  # the attempt's clock generation ended
        (_delete_farm, MINUTE, False),  # the farm is no longer eligible
        (None, timedelta(0), False),  # deadline reached
        (_disable_module, MINUTE, False),  # its module was switched off
        (_disable_and_reenable_module, MINUTE, False),  # ... and on again: a new generation
    ],
    ids=[
        "current",
        "superseded",
        "clock-switched",
        "farm-deleted",
        "deadline",
        "module-disabled",
        "module-cycled",
    ],
)
def test_only_the_current_attempt_in_its_live_generation_publishes(
    session_factory, settings, change, deadline, publishes
) -> None:
    with session_factory() as session:
        farm = Farm(name="Fenced")
        session.add(farm)
        clock.set_live(session, settings)
        session.commit()
    attempt = tasks.begin_attempt(session_factory, TASK, farm.id, deadline)
    assert attempt is not None
    if change is not None:
        change(session_factory, settings, farm.id)
    with session_factory() as session:
        assert tasks.publish_if_current(session, attempt) is publishes


def test_no_attempt_starts_while_the_clock_replays(session_factory, settings) -> None:
    with session_factory() as session:
        farm = Farm(name="Replayed")
        session.add(farm)
        clock.set_replay(session, datetime(2024, 4, 10, tzinfo=UTC), settings)
        session.commit()
    assert tasks.begin_attempt(session_factory, TASK, farm.id, MINUTE) is None


def test_a_task_enqueued_in_a_rolled_back_transaction_never_exists(session_factory) -> None:
    with session_factory() as session:
        assert tasks.enqueue(session, TASK, 1)
        session.rollback()
        assert _jobs(session) == []

        assert tasks.enqueue(session, TASK, 1)
        session.commit()
        # Already queued: refused without aborting the caller's transaction.
        assert not tasks.enqueue(session, TASK, 1)
        assert _jobs(session) == [(TASK, "todo", "test.fetch:farm=1")]


@pytest.mark.parametrize(
    ("attempts", "successor_queued", "outcome", "statuses"),
    [
        (0, False, "retried", ["todo"]),
        (0, True, "failed", ["failed", "todo"]),  # upstream #1446: retry would break the lock
        (tasks.MAX_ATTEMPTS - 1, False, "failed", ["failed"]),  # bounded retries
    ],
    ids=["retry", "successor-queued", "attempts-used-up"],
)
def test_a_dead_workers_task_is_retried_or_failed_never_left_running(
    committed_session_factory, settings, attempts, successor_queued, outcome, statuses
) -> None:
    with committed_session_factory() as session:
        tasks.enqueue(session, TASK, 1)
        dead = session.execute(
            text(
                "INSERT INTO procrastinate_workers (last_heartbeat) "
                "VALUES (now() - interval '1 hour') RETURNING id"
            )
        ).scalar_one()
        job = session.execute(
            text(
                "UPDATE procrastinate_jobs SET status = 'doing', worker_id = :w, attempts = :a "
                "RETURNING id"
            ),
            {"w": dead, "a": attempts},
        ).scalar_one()
        if successor_queued:
            assert tasks.enqueue(session, TASK, 1)
        session.commit()

    async def sweep() -> dict[int, str]:
        app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(conninfo=tasks.conninfo(settings))
        )
        async with app.open_async():
            return await tasks.retry_stalled(app.job_manager, 60)

    assert asyncio.run(sweep()) == {job: outcome}
    with committed_session_factory() as session:
        assert [status for _, status, _ in _jobs(session)] == statuses


def _finished(ago: timedelta):
    def change(factory, settings, farm_id) -> None:
        attempt = tasks.begin_attempt(factory, TASK, farm_id, MINUTE)
        with factory.begin() as session:
            assert tasks.publish_if_current(session, attempt)
            session.execute(
                text("UPDATE task_attempt SET finished_at = now() - :ago WHERE farm_id = :f"),
                {"ago": ago, "f": farm_id},
            )

    return change


def _failed(factory, settings, farm_id) -> None:
    # Its deadline already passed: only the failure itself keeps the farm from being planned.
    spec = JobSpec(
        TASK, "tests.core.fake_jobs:record", HOUR, "tests.core.fake_jobs:fail_task", ZERO
    )
    with pytest.raises(RuntimeError):
        tasks.run_task(factory, settings, spec, farm_id)


def _finished_then_switched(factory, settings, farm_id) -> None:
    _finished(timedelta(0))(factory, settings, farm_id)
    _switch_to_replay_and_back(factory, settings, farm_id)


@pytest.mark.parametrize(
    ("history", "queued"),
    [
        (None, True),  # never run: a fresh or restarted worker plans it
        (_finished(timedelta(minutes=10)), False),  # ran within the interval
        (_finished(timedelta(hours=2)), True),  # interval passed
        (_finished_then_switched, True),  # ran in replay or before the switch back to live
        (_supersede, False),  # in flight
        (_failed, False),  # a failure waits for the interval, like a run, not just the deadline
    ],
    ids=["never-run", "recent", "interval-passed", "older-generation", "in-flight", "failed"],
)
def test_planning_queues_a_farm_once_when_its_last_published_run_is_due(
    session_factory, settings, history, queued
) -> None:
    with session_factory.begin() as session:
        farm = Farm(name="Planned")
        session.add(farm)
        clock.set_live(session, settings)
    if history is not None:
        history(session_factory, settings, farm.id)
    for _ in range(2):  # every tick, every worker plans again
        with session_factory.begin() as session:
            tasks.enqueue_due(session, TASK, HOUR, [farm.id])
    with session_factory() as session:
        assert len(_jobs(session)) == (1 if queued else 0)
