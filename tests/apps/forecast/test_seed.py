"""`farmcore seed` (and the demo reset) imports the saved forecast runs from `DATA_DIR` offline;
without the folder it imports nothing and the window says "No forecast loaded"."""

from __future__ import annotations

import shutil

from sqlalchemy import func, select

from farmcore.apps.forecast import seed as forecast_seed
from farmcore.apps.forecast.models import ForecastRun
from farmcore.core import seed as core_seed
from tests.apps.forecast.test_sources import RUNS


def _runs(session) -> int:
    return session.scalar(select(func.count()).select_from(ForecastRun))


def test_seed_imports_the_saved_runs_once(session, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    shutil.copytree(RUNS, tmp_path / "open-meteo" / "ecmwf_ifs")

    first = forecast_seed.seed(session, scene="B", data_dir=tmp_path)
    again = forecast_seed.seed(session, scene="B", data_dir=tmp_path)

    assert first == {"forecast_runs": 2} and again == {"forecast_runs": 0}
    assert _runs(session) == 2


def test_seed_without_saved_runs_imports_nothing(session, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)

    assert forecast_seed.seed(session, scene="B", data_dir=tmp_path) == {"forecast_runs": 0}
    assert _runs(session) == 0
