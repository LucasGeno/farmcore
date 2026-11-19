"""`advisor.evaluate` in live mode: the worker's task writes assessments only while its attempt
is current. The fence itself is owned by `tests/core/test_tasks.py`."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from farmcore.apps.advisor.job import evaluate_live
from farmcore.apps.advisor.manifest import JOB_NAME
from farmcore.apps.stations.seed import seed as seed_station
from farmcore.core import clock, tasks
from farmcore.core import seed as core_seed
from farmcore.core.logs import lock_farm
from farmcore.core.models.canonical import Advice, Event, Parcel

PARCEL = "SRID=4326;MULTIPOLYGON(((-0.19 44.91,-0.18 44.91,-0.18 44.92,-0.19 44.91)))"


def _live_farm(session, settings, monkeypatch, tmp_path):
    """The demo farm with its station and one parcel, the clock live (the caller commits)."""
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    core_seed.seed(session, scene=None, data_dir=tmp_path)
    seed_station(session, scene=None, data_dir=tmp_path)
    farm = core_seed.ensure_default_farm(session, settings)
    session.add(Parcel(farm_id=farm.id, name="Parcel", alias="P1", area_ha=Decimal(1), geom=PARCEL))
    clock.set_live(session, settings)
    return farm


def _assessments(session, farm_id: int) -> tuple[int, int]:
    advice = session.scalar(select(func.count(Advice.id)).where(Advice.farm_id == farm_id))
    events = session.scalar(select(func.count(Event.id)).where(Event.kind == "advice.evaluated"))
    return advice, events


def _replayed(factory, settings, farm_id) -> None:
    with factory.begin() as session:
        clock.set_replay(session, datetime(2024, 4, 10, tzinfo=UTC), settings)


@pytest.mark.parametrize(
    ("change", "published"),
    [(None, 1), (_replayed, 0)],
    ids=["current", "switched-to-replay"],
)
def test_a_live_evaluation_writes_assessments_only_while_its_attempt_is_current(
    session, session_factory, settings, monkeypatch, tmp_path, change, published
) -> None:
    farm = _live_farm(session, settings, monkeypatch, tmp_path)
    session.commit()
    attempt = tasks.begin_attempt(session_factory, JOB_NAME, farm.id, timedelta(minutes=1))
    if change is not None:
        change(session_factory, settings, farm.id)

    evaluate_live(tasks.TaskContext(session_factory, settings, farm.id, attempt))

    assert _assessments(session, farm.id) == (published, published)


def test_an_attempt_whose_deadline_passes_while_it_waits_for_the_farm_writes_nothing(
    committed_session_factory, settings, monkeypatch, tmp_path
) -> None:
    """The task takes the farm's locks before the fence: the deadline is judged when the fence
    runs, not when the task's transaction began."""
    factory = committed_session_factory
    with factory.begin() as session:
        farm = _live_farm(session, settings, monkeypatch, tmp_path)
    attempt = tasks.begin_attempt(factory, JOB_NAME, farm.id, timedelta(seconds=1))

    def expired() -> bool:
        with factory() as session:
            return session.scalar(
                text("SELECT clock_timestamp() > deadline_at FROM task_attempt WHERE farm_id = :f"),
                {"f": farm.id},
            )

    def waiting() -> bool:
        with factory() as session:
            return session.scalar(
                text(
                    "SELECT count(*) > 0 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event = 'advisory'"
                )
            )

    task = threading.Thread(
        target=evaluate_live, args=(tasks.TaskContext(factory, settings, farm.id, attempt),)
    )
    with factory() as holder:  # a long operations transaction on the farm
        lock_farm(holder, farm.id)
        task.start()
        for check in (waiting, expired):  # observable states; 15 s only bounds a hang
            limit = time.monotonic() + 15
            while not check():
                assert time.monotonic() < limit, check.__name__
                time.sleep(0.05)
        holder.rollback()
    task.join(timeout=15)

    with factory() as session:
        assert _assessments(session, farm.id) == (0, 0)
