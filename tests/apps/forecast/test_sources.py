"""Open-Meteo single-run files (ECMWF IFS HRES), read offline: one run per file, its run time,
when it was available (run + ECMWF dissemination time), grid point, units and request URL."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from farmcore.apps.forecast import sources

RUNS = Path(__file__).parent / "fixtures" / "open-meteo" / "ecmwf_ifs"


def _utc(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2024, 4, day, hour, minute, tzinfo=UTC)


def test_a_run_directory_reads_each_run_with_its_times_and_provenance():
    runs = sources.SingleRunsDir(RUNS).runs()

    assert [r.issued_at for r in runs] == [_utc(9), _utc(9, 12)]
    assert [r.available for r in runs] == [_utc(9, 7, 34), _utc(9, 19, 34)]
    run = runs[1]
    assert run.as_issued
    assert run.source == "open-meteo-single-runs/ecmwf_ifs"
    assert (run.latitude, run.longitude) == (44.88576, -0.13931)  # the model grid point
    assert run.props["model"] == "ecmwf_ifs"
    assert run.props["request_url"].startswith("https://single-runs-api.open-meteo.com/")
    assert run.props["units"]["wind_speed_10m"] == "km/h"


def test_null_first_steps_are_missing_and_gusts_are_kept_apart():
    run = sources.SingleRunsDir(RUNS).runs()[1]
    rain = {ts: v for ts, var, v in run.values if var == "rr1_mm"}
    gusts = {ts: v for ts, var, v in run.values if var == "fx_kmh"}

    assert _utc(9, 12) not in rain and _utc(9, 13) in rain  # null, not zero
    assert _utc(9, 12) not in gusts and gusts[_utc(10, 8)] == 35.0


def test_a_wind_unit_other_than_km_h_is_refused(tmp_path):
    doc = json.loads((RUNS / "run-2024-04-09T12.json").read_text(encoding="utf-8"))
    doc["response"]["hourly_units"]["wind_speed_10m"] = "m/s"
    (tmp_path / "run.json").write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ValueError, match="km/h"):
        sources.SingleRunsDir(tmp_path).runs()
