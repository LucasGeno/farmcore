"""D3: "Tested on 6 seasons" on Season & impact, visible in Simple view too. 2020-2023 come from
the backtest; 2024-2025 from the KPI season runs plus the BSV rows. Wording: tested, consistent
with the regional bulletin; never accuracy or validated."""

from __future__ import annotations

import re

import pytest

from farmcore.apps.kpi import backtest, season
from tests.apps.kpi.synth import seed_farm as _farm
from tests.apps.kpi.synth import write_station


@pytest.fixture
def station(kpi_settings, session):
    path = write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)
    return season.station_file(kpi_settings.data_dir) if path is None else path


def test_six_seasons_2020_to_2025_with_saved_passes_and_a_bulletin_check(kpi_settings, station):
    rows = backtest.tested_seasons(season.station_file(kpi_settings.data_dir))
    assert [r.year for r in rows] == [2020, 2021, 2022, 2023, 2024, 2025]
    for r in rows:
        assert r.saved == r.calendar_passes - r.advisor_passes
        assert r.check in {"consistent", "not consistent", "not comparable", "data gap"}
    by_year = {r.year: r for r in rows}
    assert by_year[2022].check == "not comparable" and by_year[2025].check == "not comparable"
    # 2021's rain gap before the first alert is a data gap, not a pass or a fail.
    assert by_year[2021].check == "data gap"


def test_2024_and_2025_use_the_existing_kpi_season_runs(kpi_settings, station):
    path = season.station_file(kpi_settings.data_dir)
    rows = {r.year: r for r in backtest.tested_seasons(path)}
    for year, run in season.season_results(path).items():
        assert rows[year].advisor_passes == run.model.passes
        assert rows[year].calendar_passes == run.calendar.passes


def test_2024_window_is_the_16_and_23_april_dates_and_2025_is_period_only():
    assert backtest.BSV[2024].window == (backtest.date(2024, 4, 16), backtest.date(2024, 4, 23))
    assert backtest.BSV[2025].window is None and "period only" in backtest.BSV[2025].precision


@pytest.mark.parametrize("expert", [False, True])
def test_page_shows_the_table_in_simple_and_expert(kpi_client, kpi_settings, station, expert):
    if expert:
        kpi_client.cookies.set("expert", "1")
    page = kpi_client.get("/kpi/").text
    assert "Tested on 6 seasons" in page and "data-tested-seasons" in page
    for year in range(2020, 2026):
        assert f'data-tested-year="{year}"' in page
    assert ("data-backtest" in page) is expert  # the per-season details stay in Expert
    text = re.sub(r"<[^>]+>", " ", page).lower()
    assert "consistent" in text and "not comparable" in text and "data gap" in text
    for banned in ("accuracy", "validated", "validation"):
        assert banned not in text.replace("backtest, not validation", "")
    for note in ("first visible symptoms", "agronomist to confirm", "rain"):
        assert note in text


def test_json_contract_is_unchanged(kpi_client, kpi_settings, station):
    assert "tested" not in kpi_client.get("/kpi/kpis.json").json()


def test_2024_dates_line_is_facts_only_and_matches_the_checked_dates(kpi_settings, station):
    rows = {r.year: r for r in backtest.tested_seasons(season.station_file(kpi_settings.data_dir))}
    # Synthetic station here: the format and the arithmetic are what is under test.
    line = rows[2024].dates
    assert (
        line.startswith("First alert ")
        and " · bulletin first symptoms 16 Apr · model estimate " in line
    )
    assert "consistent" not in line.lower() and "verdict" not in line.lower()
    assert rows[2022].dates == "" and rows[2025].dates == ""  # period only: nothing to place


def test_dates_line_for_the_real_2024_run_reads_9_apr_16_apr_2_may():
    station = _real_station()
    if not station.is_file():
        pytest.skip(f"open demo station file not present: {station.name}")
    run = backtest.run_season(2024, station)
    assert backtest.dates_line(run) == (
        "First alert 9 Apr · bulletin first symptoms 16 Apr · "
        "model estimate 2 May (9 days after the bulletin window)"
    )


def test_dates_line_wording_before_inside_and_after():
    from dataclasses import replace
    from datetime import date

    run = backtest.run_season(2020)
    first, last = run.bsv.window
    for predicted, tail in (
        (date(2020, 4, 10), "(5 days before the bulletin window)"),
        (date(2020, 4, 20), "(inside the bulletin window)"),
        (date(2020, 5, 1), "(4 days after the bulletin window)"),
    ):
        line = backtest.dates_line(replace(run, predicted_symptoms=predicted))
        assert line.endswith(tail) and "bulletin first symptoms 15 Apr" in line


@pytest.mark.parametrize("expert", [False, True])
def test_page_shows_2024_under_the_table_and_the_rest_only_in_expert(
    kpi_client, kpi_settings, station, expert
):
    if expert:
        kpi_client.cookies.set("expert", "1")
    page = kpi_client.get("/kpi/").text
    years = set(re.findall(r'data-dates-year="(\d{4})"', page))
    assert years == ({"2020", "2021", "2023", "2024"} if expert else {"2024"})


def _real_station():
    from tests.conftest import DATA_DIR

    return season.station_file(DATA_DIR)
