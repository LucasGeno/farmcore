"""Durable tasks: the publication fence, transactional enqueue and stalled-task retry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import procrastinate
import pytest
from sqlalchemy import text

from farmcore.core import clock, tasks
from farmcore.core.models.canonical import Farm

TASK = "test.fetch"
MINUTE = timedelta(minutes=1)


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
    ],
    ids=["current", "superseded", "clock-switched", "farm-deleted", "deadline"],
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
