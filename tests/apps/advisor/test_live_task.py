"""`advisor.evaluate` in live mode: the worker's task writes assessments only while its attempt
is current. The fence itself is owned by `tests/core/test_tasks.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from farmcore.apps.advisor.job import evaluate_live
from farmcore.apps.advisor.manifest import JOB_NAME
from farmcore.apps.stations.seed import seed as seed_station
from farmcore.core import clock, tasks
from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import Advice, Event, Parcel

PARCEL = "SRID=4326;MULTIPOLYGON(((-0.19 44.91,-0.18 44.91,-0.18 44.92,-0.19 44.91)))"


@pytest.fixture
def farm(session, settings, monkeypatch, tmp_path):
    """The demo farm with its station and one parcel, the clock live."""
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    core_seed.seed(session, scene=None, data_dir=tmp_path)
    seed_station(session, scene=None, data_dir=tmp_path)
    farm = core_seed.ensure_default_farm(session, settings)
    session.add(Parcel(farm_id=farm.id, name="Parcel", alias="P1", area_ha=Decimal(1), geom=PARCEL))
    clock.set_live(session, settings)
    session.commit()
    return farm


def _superseded(factory, settings, farm_id) -> None:
    tasks.begin_attempt(factory, JOB_NAME, farm_id, timedelta(minutes=1))


def _replayed(factory, settings, farm_id) -> None:
    with factory.begin() as session:
        clock.set_replay(session, datetime(2024, 4, 10, tzinfo=UTC), settings)


@pytest.mark.parametrize(
    ("change", "published"),
    [(None, 1), (_superseded, 0), (_replayed, 0)],
    ids=["current", "superseded", "switched-to-replay"],
)
def test_a_live_evaluation_writes_assessments_only_while_its_attempt_is_current(
    session, session_factory, settings, farm, change, published
) -> None:
    attempt = tasks.begin_attempt(session_factory, JOB_NAME, farm.id, timedelta(minutes=1))
    if change is not None:
        change(session_factory, settings, farm.id)

    evaluate_live(tasks.TaskContext(session_factory, settings, farm.id, attempt))

    advice = session.scalar(select(func.count(Advice.id)).where(Advice.farm_id == farm.id))
    events = session.scalar(select(func.count(Event.id)).where(Event.kind == "advice.evaluated"))
    assert (advice, events) == (published, published)
