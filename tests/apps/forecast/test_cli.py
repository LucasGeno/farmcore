"""`farmcore forecast import --file <path>`: the only way a forecast reaches the database."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from farmcore.apps.forecast.models import ForecastRun
from farmcore.cli import cli
from farmcore.core.seed import ensure_default_farm
from farmcore.settings import get_settings
from tests.apps.forecast.conftest import FIXTURE
from tests.apps.forecast.test_sources import RUNS


@pytest.fixture
def env(committed_session_factory, settings, tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no .env: the command reads the env
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    with committed_session_factory() as s:
        ensure_default_farm(s, settings)
        s.commit()
    try:
        yield
    finally:
        get_settings.cache_clear()


def test_import_prints_counts_and_issue_times_and_is_idempotent(env, committed_session_factory):
    runner = CliRunner()

    first = runner.invoke(cli, ["forecast", "import", "--file", str(FIXTURE)])
    second = runner.invoke(cli, ["forecast", "import", "--file", str(FIXTURE)])

    assert first.exit_code == 0, first.output
    assert "2 runs imported (1344 values)" in first.output
    assert "issued 2024-04-08 00:00 UTC .. 2024-04-09 00:00 UTC" in first.output
    assert "0 runs imported (0 values), 2 already held" in second.output
    with committed_session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ForecastRun)) == 2


def test_a_missing_file_fails_without_writing(env, tmp_path):
    out = CliRunner().invoke(cli, ["forecast", "import", "--file", str(tmp_path / "none.json")])

    assert out.exit_code == 1
    assert "forecast import failed" in out.output


def test_fetch_saves_one_file_per_run_and_skips_runs_already_saved(env, tmp_path, monkeypatch):
    """The only network step (the public Open-Meteo API, mocked here); import reads the files."""
    from farmcore.apps.forecast import sources

    calls: list[str] = []
    sample = json.loads((RUNS / "run-2024-04-09T12.json").read_text(encoding="utf-8"))

    def fake_get(url: str) -> dict:
        calls.append(url)
        return sample["response"]

    monkeypatch.setattr(sources, "get_json", fake_get)
    out = tmp_path / "runs"
    args = ["forecast", "fetch", "--from", "2024-04-09", "--to", "2024-04-09", "--out", str(out)]

    first = CliRunner().invoke(cli, args)
    second = CliRunner().invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert sorted(p.name for p in out.glob("*.json")) == [
        "run-2024-04-09T00.json",
        "run-2024-04-09T12.json",
    ]
    assert len(calls) == 2 and "run=2024-04-09T00:00" in calls[0]
    assert "models=ecmwf_ifs" in calls[0] and "timezone=UTC" in calls[0]
    assert "latitude=44.917667&longitude=-0.188" in calls[0]  # the station
    saved = json.loads((out / "run-2024-04-09T00.json").read_text(encoding="utf-8"))
    assert saved["run"] == "2024-04-09T00:00" and saved["request_url"] == calls[0]
    assert "2 runs saved" in first.output and "0 runs saved, 2 already saved" in second.output


def test_import_reads_a_folder_of_saved_runs(env, committed_session_factory):
    out = CliRunner().invoke(cli, ["forecast", "import", "--dir", str(RUNS)])

    assert out.exit_code == 0, out.output
    assert "2 runs imported" in out.output
    assert "issued 2024-04-09 00:00 UTC .. 2024-04-09 12:00 UTC" in out.output
