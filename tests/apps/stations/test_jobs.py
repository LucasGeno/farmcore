"""`stations.ingest_meteofrance`: rows up to clock now + 73 h, incremental, nulls skipped.

73 h = the 72 h replay forecast + the hour the advisor window's rain lookahead reads past it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from farmcore.apps.stations.jobs import ingest_meteofrance
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import AccessLog, Asset, Observation
from tests.apps.stations.synth import FILE, hourly, write_csv

# Replay now 2024-04-10 06:00 Europe/Paris (CEST) = 04:00 UTC.
NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
HORIZON = NOW + timedelta(hours=73)
START = datetime(2024, 4, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def ctx_at(session, settings, tmp_path):
    job_settings = settings.model_copy(update={"data_dir": tmp_path})

    def make(now: datetime) -> JobContext:
        return JobContext(session=session, now=now, settings=job_settings)

    return make


def _obs(session) -> list[Observation]:
    return list(session.scalars(select(Observation).order_by(Observation.ts, Observation.variable)))


def _station(session) -> Asset:
    return session.scalar(select(Asset).where(Asset.kind == "station"))


def test_job_ingests_up_to_now_plus_73h_then_only_newer_rows(session, seeded, ctx_at, tmp_path):
    seeded()
    write_csv(tmp_path, hourly(START, 240))  # 8 Apr 00:00 .. 17 Apr 23:00 UTC

    ingest_meteofrance(ctx_at(NOW))

    obs = _obs(session)
    assert max(o.ts for o in obs) == HORIZON
    # 8 Apr 00:00 .. 13 Apr 05:00 UTC inclusive = 126 hours, 4 variables each.
    assert len(obs) == 126 * 4
    assert {o.variable for o in obs} == {"rr1_mm", "t_c", "rh_pct", "ff_kmh"}

    ingest_meteofrance(ctx_at(NOW))  # re-run at the same now
    assert len(_obs(session)) == 126 * 4

    ingest_meteofrance(ctx_at(NOW + timedelta(hours=1)))  # one step later
    obs = _obs(session)
    assert len(obs) == 127 * 4
    assert max(o.ts for o in obs) == HORIZON + timedelta(hours=1)


def test_null_cells_give_no_row_and_health_counts_them(session, seeded, ctx_at, tmp_path):
    seeded()
    rows = hourly(START, 4)
    rows[1] = (rows[1][0], "", "11.0", "85", "6.0")  # rain missing
    rows[2] = (rows[2][0], "1.4", "", "90", "")  # temperature and wind missing
    write_csv(tmp_path, rows)

    ingest_meteofrance(ctx_at(NOW))

    obs = _obs(session)
    assert len(obs) == 4 * 4 - 3
    assert not any(o.value == 0 and o.ts == rows[1][0] and o.variable == "rr1_mm" for o in obs)
    assert {(o.ts, o.variable) for o in obs if o.ts in (rows[1][0], rows[2][0])} == {
        (rows[1][0], "t_c"),
        (rows[1][0], "rh_pct"),
        (rows[1][0], "ff_kmh"),
        (rows[2][0], "rr1_mm"),
        (rows[2][0], "rh_pct"),
    }
    health = _station(session).props
    assert health["nulls_skipped"] == 3
    assert health["rows"] == 13
    assert health["last_ts"] == rows[3][0].isoformat()
    assert health["last_run"] == NOW.isoformat()
    assert health["file"] == FILE


def test_job_reads_through_a_scoped_repository(session, seeded, ctx_at, tmp_path):
    seeded()
    write_csv(tmp_path, hourly(START, 2))

    ingest_meteofrance(ctx_at(NOW))

    rows = list(session.scalars(select(AccessLog)))
    assert rows
    assert {(r.actor, r.scope, r.purpose) for r in rows} == {
        ("job:stations.ingest_meteofrance", "stations:read", "job")
    }


def test_job_without_a_seeded_station_does_nothing(session, ctx_at, tmp_path):
    write_csv(tmp_path, hourly(START, 2))

    ingest_meteofrance(ctx_at(NOW))

    assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_stepping_back_bounds_the_health_to_the_new_now(session, seeded, ctx_at, tmp_path):
    seeded()
    rows = hourly(START, 240)
    rows[1] = (rows[1][0], "", "11.0", "85", "6.0")  # one null early on
    rows[200] = (rows[200][0], "", "11.0", "85", "6.0")  # one null well past NOW + 73 h
    write_csv(tmp_path, rows)
    ingest_meteofrance(ctx_at(START + timedelta(hours=239)))  # far step: everything stored

    ingest_meteofrance(ctx_at(NOW))  # step back

    health = _station(session).props
    assert health["last_ts"] == HORIZON.isoformat()
    assert health["rows"] == 126 * 4 - 1
    assert health["nulls_skipped"] == 1
    assert health["last_run"] == NOW.isoformat()


def test_missing_file_records_the_error_in_health(session, seeded, ctx_at):
    seeded()

    ingest_meteofrance(ctx_at(NOW))  # no CSV in data_dir

    health = _station(session).props
    assert health["error"] == "file missing"
    assert health["last_run"] == NOW.isoformat()
    assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_file_outside_data_dir_is_refused(session, seeded, ctx_at, tmp_path):
    seeded()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
    write_csv(tmp_path, hourly(START, 2)).rename(outside)
    station = _station(session)
    station.props = {**station.props, "file": str(outside)}
    session.flush()

    ingest_meteofrance(ctx_at(NOW))

    assert _station(session).props["error"] == "file missing"
    assert session.scalar(select(func.count()).select_from(Observation)) == 0
    outside.unlink()


def test_a_good_run_clears_an_earlier_error(session, seeded, ctx_at, tmp_path):
    seeded()
    ingest_meteofrance(ctx_at(NOW))
    write_csv(tmp_path, hourly(START, 2))

    ingest_meteofrance(ctx_at(NOW))

    assert "error" not in _station(session).props
