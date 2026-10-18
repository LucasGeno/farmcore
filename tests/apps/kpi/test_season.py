"""`kpi.season.season_results` on a synthetic station file in `tmp_path`."""

from __future__ import annotations

import os
from dataclasses import replace

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import calendar_passes, season_span
from farmcore.apps.kpi import season
from tests.apps.kpi.synth import write_station


def _spy(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = season.simulate

    def spy(series, cfg, start, end):
        calls.append(cfg.year)
        return real(series, cfg, start, end)

    monkeypatch.setattr(season, "simulate", spy)
    return calls


def test_each_year_is_the_shared_span_simulation_and_calendar(tmp_path):
    path = write_station(tmp_path)

    results = season.season_results(path)

    assert sorted(results) == [2024, 2025]
    for year, r in results.items():
        cfg = AdvisorConfig.for_region_year("FRI12", year)
        assert (r.model.start, r.model.end) == season_span(cfg)
        assert r.calendar == calendar_passes(cfg, year)
        assert (r.model.form, r.model.days_evaluated) == (
            "hourly",
            (season_span(cfg)[1] - season_span(cfg)[0]).days + 1,
        )


def test_a_season_simulates_once_per_file_version(tmp_path, monkeypatch):
    path = write_station(tmp_path)
    calls = _spy(monkeypatch)

    first = season.season_results(path)
    second = season.season_results(path)

    assert sorted(calls) == [2024, 2025]
    assert second == first
    # A rewritten file (new mtime) is a new season.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    season.season_results(path)
    assert sorted(calls) == [2024, 2024, 2025, 2025]


def test_each_year_carries_the_product_names_its_config_sprays(tmp_path, monkeypatch):
    path = write_station(tmp_path)
    real = AdvisorConfig.for_region_year.__func__

    def renamed(cls, region, year, **overrides):
        cfg = real(cls, region, year, **overrides)
        downy = replace(cfg.default_downy_product, name=f"cu-{year}")
        return cfg.model_copy(update={"default_downy_product": downy})

    monkeypatch.setattr(AdvisorConfig, "for_region_year", classmethod(renamed))

    results = season.season_results(path)

    for year, r in results.items():
        cfg = AdvisorConfig.for_region_year("FRI12", year)
        assert r.copper == f"cu-{year}"
        assert r.sulphur == cfg.default_powdery_product.name
