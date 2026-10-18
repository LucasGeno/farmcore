"""N11: a France-only backtest over Météo-France 33394002, 2020–2023, on the pure engine.

Advisor passes vs the existing calendar programme per season, and the model's first downy alert
against the BSV first-symptom window. A backtest, not validation: never an accuracy figure.
"""

from __future__ import annotations

import csv
import re
from datetime import date

import pytest
from typer.testing import CliRunner

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import calendar_passes
from farmcore.apps.kpi import backtest, config
from farmcore.cli import cli
from tests.apps.kpi.synth import seed_farm as _farm
from tests.apps.kpi.synth import write_station


@pytest.fixture(scope="module")
def results():
    return backtest.run_all()


def test_extract_is_station_33394002_march_to_september_2020_to_2023():
    with backtest.SEASON_FILE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert {r["num_poste"] for r in rows} == {"33394002"}
    assert list(rows[0]) == [
        "num_poste", "nom_usuel", "lat", "lon", "alti_m", "timestamp_utc",
        "rr1_mm", "t_c", "rh_pct", "ff_ms", "ff_kmh", "fxi_ms",
    ]  # fmt: skip
    years = {r["timestamp_utc"][:4] for r in rows}
    assert years == {"2020", "2021", "2022", "2023"}
    months = {int(r["timestamp_utc"][5:7]) for r in rows}
    assert months == {3, 4, 5, 6, 7, 8, 9}
    assert len(rows) == 4 * 5136  # every hour of 1 Mar - 30 Sep is a row; gaps are empty cells


def test_each_season_has_model_and_calendar_passes_from_the_pure_engine(results):
    assert [r.year for r in results] == [2020, 2021, 2022, 2023]
    for r in results:
        cfg = AdvisorConfig.for_region_year(config.REGION, r.year)
        cal = calendar_passes(cfg, r.year)
        assert (r.calendar_passes, r.calendar_treatments) == (cal.passes, cal.treatments)
        assert r.model_passes >= 0 and r.model_treatments >= r.model_passes
        assert r.start == cfg.season_gate.value and r.end == cfg.calendar.bunch_closure.value
        assert r.season_assumed is True  # 2020-23 use the default season dates: assumed


def test_first_downy_alert_is_in_the_season_and_never_before_the_gate(results):
    for r in results:
        if r.first_alert is not None:
            assert r.start <= r.first_alert <= r.end
        if r.first_alert is not None and r.predicted_symptoms is not None:
            assert r.predicted_symptoms >= r.first_alert


def test_bsv_rows_are_verbatim_with_a_source_each():
    rows = backtest.BSV
    assert sorted(rows) == [2020, 2021, 2022, 2023, 2024, 2025]  # 2024-25: D3's table rows
    assert "**15 April 2020** (single isolated plot, Centre-Médoc)" in rows[2020].quote
    assert "23–27 April 2020" in rows[2020].quote
    assert "Mid-May 2021 (week of 17–20 May 2021)" in rows[2021].quote
    assert "Late April to early May 2022" in rows[2022].quote
    assert "**20 April 2023** (first sentinel TNT spots)" in rows[2023].quote
    for row in rows.values():
        assert row.url.startswith("https://") and "BSV" in row.source or "Vinopôle" in row.source
    assert "**16 April 2024** (outside BSV network)" in rows[2024].quote
    assert "Late April to early May 2025" in rows[2025].quote
    assert rows[2022].comparable is False  # period only: never compared
    assert rows[2025].comparable is False
    assert rows[2020].window == (date(2020, 4, 15), date(2020, 4, 27))
    assert rows[2021].window == (date(2021, 5, 17), date(2021, 5, 20))
    assert rows[2023].window == (date(2023, 4, 20), date(2023, 5, 3))


def test_verdict_uses_the_assumed_tolerance_and_never_says_accuracy(results):
    bsv = backtest.BSV
    inside = backtest.verdict(date(2023, 4, 30), bsv[2023])
    assert inside == backtest.CONSISTENT
    assert backtest.verdict(date(2023, 6, 30), bsv[2023]) == backtest.NOT_CONSISTENT
    assert backtest.verdict(None, bsv[2023]) == backtest.NOT_CONSISTENT
    assert backtest.verdict(date(2022, 5, 1), bsv[2022]) == backtest.NO_WINDOW
    for r in results:
        assert r.verdict.removesuffix(backtest.DATA_GAP_SUFFIX) in {
            backtest.CONSISTENT,
            backtest.NOT_CONSISTENT,
            backtest.NO_WINDOW,
        }
    text = " ".join(
        [backtest.LABEL, backtest.CONSISTENCY_LABEL, backtest.ASSUMPTION_NOTE, backtest.DISCLAIMER]
    ).lower()
    assert "backtest, not validation" in text
    assert "symptoms, not infection dates; network-wide" in text
    assert "assumption, agronomist to confirm" in text
    for banned in ("accuracy", "% correct", "percent correct", "precision"):
        assert banned not in text


def test_data_gaps_are_reported_not_filled(results):
    by_year = {r.year: r for r in results}
    assert by_year[2021].rain_gap_hours == 72  # 27-30 Apr 2021: RR1 missing at the source
    assert by_year[2020].rain_gap_hours == 0


def test_cli_backtest_season_prints_the_row_and_touches_no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none")
    out = CliRunner().invoke(cli, ["backtest", "--season", "2021"])
    assert out.exit_code == 0, out.output
    assert "2021" in out.output and "backtest, not validation" in out.output
    assert "calendar" in out.output.lower() and "first downy alert" in out.output.lower()
    bad = CliRunner().invoke(cli, ["backtest", "--season", "2019"])
    assert bad.exit_code != 0


def test_season_and_impact_shows_the_backtest_to_experts_only(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    simple = kpi_client.get("/kpi/").text
    assert "backtest" not in simple.lower()

    kpi_client.cookies.set("expert", "1")
    html = kpi_client.get("/kpi/").text
    section = re.search(r"<section[^>]*data-backtest.*?</section>", html, re.S).group(0)
    assert "backtest, not validation" in section
    assert backtest.CONSISTENCY_LABEL in section
    for year in (2020, 2021, 2022, 2023):
        assert f'data-backtest-year="{year}"' in section
    assert "assumption, agronomist to confirm" in section
    lowered = section.lower()
    assert "accuracy" not in lowered and "% correct" not in lowered
    assert "https://" in section  # a source link per BSV row


def test_the_backtest_never_appears_on_a_farm_without_the_french_station(
    kpi_client, kpi_settings, session
):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings, station=False)
    kpi_client.cookies.set("expert", "1")
    assert "backtest" not in kpi_client.get("/kpi/").text.lower()


def test_2021_rain_gap_before_the_alert_makes_the_verdict_explicit(results):
    by_year = {r.year: r for r in results}
    r = by_year[2021]
    assert r.rain_gap_hours == 72 and r.rain_gap_before_alert == 72
    assert r.verdict == "consistent (data gap: earlier alert not ruled out)"
    assert "72" in r.gap_notes[0] and "27 Apr" in r.gap_notes[0] and "30 Apr" in r.gap_notes[0]
    for year in (2020, 2022, 2023):
        assert by_year[year].rain_gap_before_alert == 0
        assert "data gap" not in by_year[year].verdict


def test_2023_humidity_gap_is_a_per_season_note(results):
    by_year = {r.year: r for r in results}
    r = by_year[2023]
    assert r.rh_gap_extract_hours == 205  # 1 Mar - 11 Apr 2023, all before the 15 Apr gate
    assert r.rh_gap_hours == 0
    note = next(n for n in r.gap_notes if "humidity" in n)
    assert "205 hours" in note and "none inside the evaluated season span" in note
    assert not any("humidity" in n for n in by_year[2020].gap_notes)
    assert by_year[2022].rh_gap_extract_hours == 1


def test_bsv_provenance_line_and_week_level_mark_are_on_screen_and_in_the_cli(
    kpi_client, kpi_settings, session, monkeypatch
):
    line = "BSV dates from public bulletins via secondary research; exact days not all verified"
    assert line == backtest.BSV_PROVENANCE
    assert backtest.BSV[2021].precision == "week-level (exact day not verified)"
    assert backtest.BSV[2022].precision.startswith("period only")

    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)
    kpi_client.cookies.set("expert", "1")
    html = kpi_client.get("/kpi/").text
    section = re.search(r"<section[^>]*data-backtest.*?</section>", html, re.S).group(0)
    assert line in section
    card = re.search(r'<article[^>]*data-backtest-year="2021".*?</article>', section, re.S)
    assert "week-level (exact day not verified)" in card.group(0)
    assert "consistent (data gap: earlier alert not ruled out)" in card.group(0)
    assert "72 hours" in card.group(0)
    card23 = re.search(r'<article[^>]*data-backtest-year="2023".*?</article>', section, re.S)
    assert "205 hours without a humidity value" in card23.group(0)

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none")
    out = CliRunner().invoke(cli, ["backtest", "--season", "2021"]).output
    assert line in out and "week-level (exact day not verified)" in out
    assert "data gap: earlier alert not ruled out" in out and "72 hours" in out
    assert "humidity" in CliRunner().invoke(cli, ["backtest", "--season", "2023"]).output


def test_2020_quote_keeps_the_libournais_date_and_the_window_is_unchanged():
    shown = backtest.display(backtest.BSV[2020].quote)
    assert "28 April 2020 on Libournais TNT" in shown
    assert backtest.BSV[2020].window == (date(2020, 4, 15), date(2020, 4, 27))


def test_gap_notes_pluralise_hours(results):
    r = {r.year: r for r in results}[2022]
    note = next(n for n in r.gap_notes if "humidity" in n)
    assert note.startswith("1 hour without a humidity value") and "1 hours" not in note
    assert backtest.hours(1) == "1 hour" and backtest.hours(72) == "72 hours"


def test_a_single_hour_or_day_gap_is_one_date_not_a_range(results):
    from datetime import UTC, datetime

    one = datetime(2022, 4, 8, 8, tzinfo=UTC)
    assert backtest._span_text((one, one)) == "08 Apr 08:00 UTC"
    assert "08 Apr 08:00 – 08 Apr" not in backtest._span_text((one, one))
    note = next(n for n in {r.year: r for r in results}[2022].gap_notes if "humidity" in n)
    assert "08 Apr – 08 Apr" not in note and "(08 Apr UTC, not filled)" in note
    two = datetime(2022, 4, 8, 9, tzinfo=UTC)
    assert backtest._span_text((one, two)) == "08 Apr 08:00 – 08 Apr 09:00 UTC"
