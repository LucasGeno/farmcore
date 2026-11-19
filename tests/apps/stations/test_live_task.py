"""`stations.ingest_meteofrance` in live mode: the worker's task stores observations only while
its attempt is current. The fence itself is owned by `tests/core/test_tasks.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from farmcore.apps.stations.jobs import JOB_NAME, ingest_live
from farmcore.core import clock, tasks
from farmcore.core.models.canonical import Asset, Observation
from tests.apps.stations.synth import hourly, write_csv


def _replayed(factory, settings, farm_id) -> None:
    with factory.begin() as session:
        clock.set_replay(session, datetime(2024, 4, 10, tzinfo=UTC), settings)


@pytest.mark.parametrize(
    ("change", "stored"),
    [(None, True), (_replayed, False)],
    ids=["current", "switched-to-replay"],
)
def test_a_live_ingest_stores_observations_only_while_its_attempt_is_current(
    session, session_factory, settings, seeded, tmp_path, change, stored
) -> None:
    seeded()
    write_csv(tmp_path, hourly(datetime(2024, 4, 8, tzinfo=UTC), 24))
    clock.set_live(session, settings)
    session.commit()
    farm_id = session.scalar(select(Asset.farm_id).where(Asset.kind == "station"))
    attempt = tasks.begin_attempt(session_factory, JOB_NAME, farm_id, timedelta(minutes=1))
    if change is not None:
        change(session_factory, settings, farm_id)

    live = settings.model_copy(update={"data_dir": tmp_path})
    ingest_live(tasks.TaskContext(session_factory, live, farm_id, attempt))

    assert session.scalar(select(func.count(Observation.id))) == (24 * 4 if stored else 0)
