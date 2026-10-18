"""The ingest job on the real Météo-France demo file (open data in sample-data)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from farmcore.apps.stations.jobs import ingest_meteofrance
from farmcore.apps.stations.service import load_hourly, station_for_farm
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Farm, Observation

pytestmark = pytest.mark.data

NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)  # replay now 10 Apr 2024 06:00 Europe/Paris
# The file starts 1 Mar 2024 00:00 UTC: March (744 h) + 1-12 Apr (288 h) + 13 Apr 00-05 (6 h).
HOURS_TO_HORIZON = 744 + 288 + 6


def test_real_file_ingests_to_the_horizon_and_shows_the_7_9_april_rain(
    session, settings, seeded, data_dir
):
    seeded()
    job_settings = settings.model_copy(update={"data_dir": data_dir})
    ctx = JobContext(session=session, now=NOW, settings=job_settings)

    ingest_meteofrance(ctx)

    latest = session.scalar(select(func.max(Observation.ts)))
    assert latest == NOW + timedelta(hours=73)
    farm = session.scalar(select(Farm))
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")
    station = station_for_farm(repo)
    health = station.props
    assert health["rows"] + health["nulls_skipped"] == HOURS_TO_HORIZON * 4
    assert health["last_ts"] == latest.isoformat()

    rain = load_hourly(
        repo, station.id, datetime(2024, 4, 7, tzinfo=UTC), datetime(2024, 4, 9, 23, tzinfo=UTC)
    )
    assert sum(r["rr1_mm"] or 0 for r in rain) > 10  # the 7-9 Apr 2024 rain (~11.7 mm)

    ingest_meteofrance(ctx)
    assert session.scalar(select(func.count()).select_from(Observation)) == health["rows"]
