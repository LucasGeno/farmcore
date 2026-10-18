"""Imported forecasts keep their issue time; a read at clock time `now` sees only the latest run
issued at or before `now` (replay honesty), never a later one."""

from __future__ import annotations

from datetime import UTC, datetime

from farmcore.apps.forecast import sources, store
from tests.apps.forecast.conftest import FIXTURE

HOURS = 7 * 24  # each fixture run
VARIABLES = 4  # rain, wind, temperature, humidity


def _utc(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 4, day, hour, tzinfo=UTC)


def _rain_on(run: store.StoredRun, local_day: int) -> float:
    """Rain over a Paris day (CEST, UTC+2) in the run's hours."""
    return sum(o.rr1_mm or 0.0 for o in run.series if (o.ts.hour + 2 >= 24) + o.ts.day == local_day)


def test_import_stores_each_run_once_with_its_issue_time(session, farm):
    runs = sources.FileSource(FIXTURE).runs()

    first = store.import_runs(session, farm.id, runs)
    again = store.import_runs(session, farm.id, runs)

    assert [r.issued_at for r in runs] == [_utc(8), _utc(9)]
    assert first == {"runs": 2, "values": 2 * HOURS * VARIABLES}
    assert again == {"runs": 0, "values": 0}


def test_a_read_at_now_uses_only_the_latest_run_issued_by_then(session, farm, repo):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())

    assert store.latest_run(repo, _utc(7, 23)) is None
    before = store.latest_run(repo, _utc(8, 23))
    after = store.latest_run(repo, _utc(9))

    assert before.issued_at == _utc(8) and after.issued_at == _utc(9)
    assert _rain_on(before, 10) > 0 and _rain_on(after, 10) == 0  # the 9 Apr run is dry on 10 Apr
    assert after.label == "Synthetic test forecast" and after.as_issued
    assert len(after.series) == HOURS


def test_a_run_counts_from_when_it_was_available_not_from_its_run_time(session, farm, repo):
    """ECMWF runs reach users hours after their run time: a 00Z run available at 07:34 is not
    yet there at 06:00 Paris (04Z), so the previous run is used."""
    runs = [
        sources.Run(
            "om",
            _utc(8, 12),
            "test",
            True,
            None,
            None,
            [(_utc(10, 6), "rr1_mm", 1.0)],
            available_at=_utc(8, 19),
        ),
        sources.Run(
            "om",
            _utc(9),
            "test",
            True,
            None,
            None,
            [(_utc(10, 6), "rr1_mm", 2.0)],
            available_at=_utc(9, 7),
        ),
    ]
    store.import_runs(session, farm.id, runs)

    at_six = store.latest_run(repo, _utc(9, 4))
    at_eight = store.latest_run(repo, _utc(9, 8))

    assert at_six.issued_at == _utc(8, 12) and at_six.available_at == _utc(8, 19)
    assert at_eight.issued_at == _utc(9)
    assert store.latest_run(repo, _utc(8, 18)) is None


def test_a_stored_single_run_keeps_gusts_apart_and_its_provenance(session, farm, repo):
    from tests.apps.forecast.test_sources import RUNS

    store.import_runs(session, farm.id, sources.SingleRunsDir(RUNS).runs())

    run = store.latest_run(repo, _utc(9, 20))

    assert run.issued_at == _utc(9, 12)
    assert run.gusts[_utc(10, 8)] == 35.0
    assert run.props["request_url"].startswith("https://single-runs-api.open-meteo.com/")
    assert "ECMWF IFS HRES (CC BY 4.0)" in run.props["attribution"]


def test_a_run_that_ends_before_now_counts_as_no_run(session, farm, repo):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())

    assert store.latest_run(repo, _utc(15, 23)) is not None  # the 9 Apr run's last hour
    assert store.latest_run(repo, _utc(16, 1)) is None
