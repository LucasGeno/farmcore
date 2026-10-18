"""Season simulation and the calendar-pass estimate, through `simulate` and
`calendar_passes`. Synthetic series are invented values (synth.py)."""

import time
from datetime import date, timedelta

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import calendar_passes, season_span, simulate
from tests.apps.advisor.engine.daily_synth import days
from tests.apps.advisor.engine.synth import merge, rain, series, utc

CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)


def test_calendar_passes_2024_count_dates_and_merges():
    # Hand count, 2024: gate 5 Apr, rapid growth 20 Apr–30 Jun, bunch closure 8 Jul.
    # Downy every 10 d outside rapid growth, 7 d inside: 5/15/25 Apr, 2/9/16/23/30 May,
    # 6/13/20/27 Jun, 4 Jul (13). Sulphur every 14 d: 5/19 Apr, 3/17/31 May, 14/28 Jun (7), each
    # riding the nearest downy pass (tank-mix): 5 Apr, 15 Apr, 2 May, 16 May, 30 May, 13 Jun,
    # 27 Jun. 13 passes, 13 + 7 = 20 treatments (≈ Agreste 20.1).
    result = calendar_passes(CONFIG, 2024)

    assert result.passes == 13
    assert result.treatments == 20
    assert [p.day for p in result.dates] == [
        date(2024, 4, 5),
        date(2024, 4, 15),
        date(2024, 4, 25),
        date(2024, 5, 2),
        date(2024, 5, 9),
        date(2024, 5, 16),
        date(2024, 5, 23),
        date(2024, 5, 30),
        date(2024, 6, 6),
        date(2024, 6, 13),
        date(2024, 6, 20),
        date(2024, 6, 27),
        date(2024, 7, 4),
    ]
    merged = {p.day for p in result.dates if p.products == ("copper", "sulphur")}
    assert merged == {
        date(2024, 4, 5),
        date(2024, 4, 15),
        date(2024, 5, 2),
        date(2024, 5, 16),
        date(2024, 5, 30),
        date(2024, 6, 13),
        date(2024, 6, 27),
    }
    assert all(p.products in (("copper",), ("copper", "sulphur")) for p in result.dates)
    assert (result.start, result.end) == (date(2024, 4, 5), date(2024, 7, 8))
    assert "estimate" in result.label
    assert "season gate" in result.label and "bunch closure" in result.label
    assert calendar_passes(CONFIG, 2024) == result  # deterministic


def test_calendar_passes_2025_count():
    # 2025: gate 15 Apr, rapid growth 25 Apr–30 Jun, bunch closure 5 Jul. Downy 15/25 Apr,
    # 2/9/16/23/30 May, 6/13/20/27 Jun, 4 Jul (12); sulphur 15/29 Apr, 13/27 May, 10/24 Jun (6).
    result = calendar_passes(AdvisorConfig.for_region_year("FRI12", 2025), 2025)

    assert (result.passes, result.treatments) == (12, 18)
    assert (result.start, result.end) == (date(2025, 4, 15), date(2025, 7, 5))


def _calendar(**values):
    cal = CONFIG.calendar
    moved = {k: getattr(cal, k).model_copy(update={"value": v}) for k, v in values.items()}
    return CONFIG.model_copy(update={"calendar": cal.model_copy(update=moved)})


def test_calendar_passes_follow_the_configured_intervals():
    # Downy every 10 d throughout: 5/15/25 Apr, 5/15/25 May, 4/14/24 Jun, 4 Jul (10). Sulphur
    # 5/19 Apr, 3/17/31 May, 14/28 Jun: nearest downy within 4 d each → all ride (10p/17t).
    flat = calendar_passes(_calendar(downy_interval_rapid_growth_days=10.0), 2024)
    # merge_days 0: only same-day sulphur rides (5 Apr, 14 Jun); 5 sulphur-only passes.
    apart = calendar_passes(_calendar(downy_interval_rapid_growth_days=10.0, merge_days=0.0), 2024)

    assert (flat.passes, flat.treatments) == (10, 17)
    assert (apart.passes, apart.treatments) == (15, 17)
    assert ("sulphur",) in [p.products for p in apart.dates]


# 2024 config: gate 5 Apr, rapid growth 20 Apr–30 Jun (copper lasts 7 d), bloom 28 May.
# Two rain events, 2 mm/h for 6 h from 12Z: R48 reaches 10 mm at 16Z (3-10 event).
EVENT_1 = rain(utc(2024, 4, 20, 12), 2.0, 6)  # before bloom: copper alone
EVENT_2 = rain(utc(2024, 5, 27, 12), 2.0, 6)  # eve of bloom: powdery never applied → tank-mix


def _season(*overrides):
    return series(utc(2024, 4, 1), utc(2024, 6, 14), overrides=merge(*overrides))


def test_two_rain_events_give_two_passes_and_the_tank_mix_counts_two_treatments():
    result = simulate(_season(EVENT_1, EVENT_2), CONFIG, date(2024, 4, 1), date(2024, 6, 10))

    assert result.passes == 2
    assert result.treatments == 3
    first, second = result.dates
    # Evaluated at 06:00 local (04Z, CEST); calm and dry, so the window is open at once.
    assert first.ts == utc(2024, 4, 21, 4)
    assert first.products == ("copper",)
    assert first.treatments == 1
    assert second.ts == utc(2024, 5, 28, 4)
    assert second.products == ("copper", "sulphur")
    assert second.treatments == 2
    assert {r.rule for r in second.reasons} >= {"downy_3_10", "tankmix"}
    # Each event is active risk 3 evaluation days (downy_event_max_days): 21–23 Apr, 28–30 May.
    assert result.pressure_days == 6
    assert result.form == "hourly"


def test_a_wait_advice_sprays_at_the_first_open_window():
    windy = {utc(2024, 4, 21, h): {"ff_kmh": 30.0} for h in range(0, 10)}  # closed until 10Z

    result = simulate(_season(EVENT_1, windy), CONFIG, date(2024, 4, 1), date(2024, 5, 20))

    assert result.passes == 1
    (only,) = result.dates
    assert only.ts == utc(2024, 4, 21, 10)
    assert only.decision == "wait"
    assert only.products == ("copper",)


def test_no_rain_event_no_pass_and_every_day_is_a_window_day():
    result = simulate(_season(), CONFIG, date(2024, 4, 10), date(2024, 5, 20))

    assert result.passes == 0
    assert result.treatments == 0
    assert result.pressure_days == 0
    assert result.window_days == 41  # 10 Apr – 20 May, all calm and dry
    assert result.days_evaluated == 41


def _windy_local_hours(hours: range) -> dict:
    """Wind 30 km/h at the given local hours (CEST, UTC+2) every day of the season."""
    out = {}
    day = utc(2024, 4, 1)
    while day <= utc(2024, 6, 14):
        for h in hours:
            out[day + timedelta(hours=h - 2)] = {"ff_kmh": 30.0}
        day += timedelta(days=1)
    return out


def test_night_only_windows_are_not_window_days():
    # Calm only from 20:00 to 06:00 local: a run of open hours every night, none in daytime.
    result = simulate(
        _season(_windy_local_hours(range(6, 20))), CONFIG, date(2024, 4, 10), date(2024, 5, 20)
    )

    assert result.window_days == 0
    assert result.threshold["calendar.window_day_start_hour_local"] == 6
    assert result.threshold["calendar.window_day_end_hour_local"] == 20
    assert "daytime" in " ".join(result.labels)


def test_a_daytime_run_makes_a_window_day_even_when_nights_are_windy():
    result = simulate(
        _season(_windy_local_hours(range(20, 30))), CONFIG, date(2024, 4, 10), date(2024, 5, 20)
    )

    assert result.window_days == 41


# Daily form: invented daily values (daily_synth.py).
DAILY_EVENT = {date(2024, 4, 14): {"precip_mm": 6.0}, date(2024, 4, 15): {"precip_mm": 4.0}}


def test_simulate_runs_on_daily_data_and_sprays_on_the_next_open_day():
    windy_16th = {date(2024, 4, 16): {"wind_ms": 8.0}}
    season = days(date(2024, 4, 1), date(2024, 5, 21), overrides={**DAILY_EVENT, **windy_16th})

    result = simulate(season, CONFIG, date(2024, 4, 1), date(2024, 5, 20))

    assert result.form == "daily"
    assert result.passes == 1
    (only,) = result.dates
    assert only.decision == "wait"
    assert only.ts == utc(2024, 4, 17, 4)  # 17 Apr 06:00 local, the next open day
    assert only.products == ("copper",)
    # Closed: 13 Apr (next day 6 mm), 14 and 15 Apr (rain ≥ 2 mm), 16 Apr (wind). 50 days.
    assert result.window_days == 50 - 4
    assert result.pressure_days == 3  # event at the end of 15 Apr: evaluated 16, 17, 18 Apr
    assert "daily regional data; 2-calendar-day approximation" in result.labels


def test_mixed_series_forms_are_rejected():
    hourly = series(utc(2024, 4, 1), utc(2024, 4, 2))
    daily = days(date(2024, 4, 1), date(2024, 4, 2))

    with pytest.raises(ValueError, match="one series form"):
        simulate([*hourly, *daily], CONFIG, date(2024, 4, 1), date(2024, 4, 2))


# Data test: the hourly Météo-France station file (open data), Apr–Aug 2024.
APR_AUG = (date(2024, 4, 1), date(2024, 8, 31))


@pytest.fixture(scope="module")
def station_2024(request):
    series_ = request.getfixturevalue("station_series")
    started = time.perf_counter()
    result = simulate(series_, CONFIG, *APR_AUG)
    return result, time.perf_counter() - started


@pytest.mark.data
def test_station_2024_season_runs_apr_aug_in_under_10_s(station_2024):
    result, seconds = station_2024
    calendar = calendar_passes(CONFIG, 2024)
    print(
        f"\nstation 33394002, 2024 Apr–Aug: {seconds:.2f} s; model passes {result.passes}, "
        f"treatments {result.treatments}, pressure days {result.pressure_days}, window days "
        f"{result.window_days}/{result.days_evaluated}; calendar passes {calendar.passes} "
        f"({calendar.treatments} treatments, estimate)"
    )
    for p in result.dates:
        print(f"  {p.day} {p.decision:5} {' + '.join(p.products)}")

    assert seconds < 10
    assert result.days_evaluated == 153
    assert 0 < result.passes <= result.treatments
    assert 0 < result.pressure_days < result.days_evaluated
    assert 0 < result.window_days <= result.days_evaluated
    assert result.treatments < 20.1  # Agreste 2019 Bordelais average, the KPI baseline


@pytest.mark.data
@pytest.mark.parametrize("year", [2024, 2025])
def test_station_model_below_calendar_over_the_same_span(station_series, year):
    # Sanity check: both sides counted over season gate .. bunch
    # closure. Earlier A+B figures (11p/12t 2024, 10p/10t 2025) counted
    # the model Apr 1 – Aug 31; on this span the model gives 11p/12t vs calendar 13p/20t (2024)
    # and 8p/8t vs 12p/18t (2025), printed. Asserted only as the inequality.
    cfg = AdvisorConfig.for_region_year("FRI12", year)
    span = season_span(cfg)
    result = simulate(station_series, cfg, *span)
    calendar = calendar_passes(cfg, year)
    print(
        f"\nstation 33394002, {year} {span[0]} .. {span[1]}: model {result.passes}p/"
        f"{result.treatments}t, calendar {calendar.passes}p/{calendar.treatments}t (estimate)"
    )

    assert (result.start, result.end) == (calendar.start, calendar.end) == span
    assert result.passes < calendar.passes
    assert result.treatments < calendar.treatments
