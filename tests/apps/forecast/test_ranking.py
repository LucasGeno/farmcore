"""Graded spray window: the next 5 days ranked best / good / possible / avoid, from the advisor's
own hourly window rules (`advisor.engine.window`) over the daytime band (06:00–20:00 local), on a
forecast series. No percentages and no new thresholds."""

from __future__ import annotations

from datetime import date, timedelta

from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.forecast.ranking import rank_days
from tests.apps.advisor.engine.synth import merge, rain, series, utc

H = timedelta(hours=1)
TZ = "Europe/Paris"
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
NOW = utc(2024, 4, 9, 4)  # 9 Apr 06:00 Paris (CEST, UTC+2)
END = utc(2024, 4, 14, 4)  # past the 5th day's band and its rain lookahead


def _local(day: int, hour: int):
    """`day` Apr 2024 at `hour`:00 Paris, in UTC."""
    return utc(2024, 4, day, hour - 2)


def _wind(start, hours: int, kmh: float):
    return {start + i * H: {"ff_kmh": kmh} for i in range(hours)}


def _grades(days):
    return [(d.day.day, d.grade) for d in days]


def test_busy_best_day_moves_best_to_the_next_free_good_day():
    s = series(NOW - 3 * H, END)
    baseline = rank_days(s, NOW, TZ, CONFIG)

    days = rank_days(s, NOW, TZ, CONFIG, busy_days={date(2024, 4, 9)})

    assert _grades(days) == [(9, "busy"), (10, "best"), (11, "good"), (12, "good"), (13, "good")]
    assert days[0].reason == baseline[0].reason  # weather facts stay available
    assert days[0].open_hours == baseline[0].open_hours


def test_all_busy_has_no_best_and_ignores_past_local_dates():
    s = series(NOW - 3 * H, END)
    busy = {date(2024, 4, d) for d in range(9, 14)}
    days = rank_days(s, NOW, TZ, CONFIG, busy_days=busy)
    assert all(d.grade == "busy" for d in days)
    assert rank_days(s, NOW, TZ, CONFIG, busy_days={date(2024, 4, 8)}) == rank_days(
        s, NOW, TZ, CONFIG
    )


def test_busy_filter_never_promotes_avoid_or_missing_forecast():
    s = series(NOW - 3 * H, _local(11, 12), overrides=_wind(_local(10, 6), 14, 25.0))
    days = rank_days(s, NOW, TZ, CONFIG, busy_days={date(2024, 4, 9), date(2024, 4, 12)})
    assert _grades(days) == [
        (9, "busy"),
        (10, "avoid"),
        (11, "no_forecast"),
        (12, "busy"),
        (13, "no_forecast"),
    ]
    assert days[3].reason == "Beyond the forecast"


def test_busy_dates_use_local_midnight_and_preserve_free_day_tie_breaks():
    s = series(NOW - 24 * H, END)
    now = utc(2024, 4, 8, 22)  # already 9 Apr in Paris
    days = rank_days(s, now, TZ, CONFIG, busy_days={date(2024, 4, 9)})
    assert _grades(days)[:2] == [(9, "busy"), (10, "best")]


def test_five_days_from_today_with_the_longest_open_run_best_and_rain_avoid():
    s = series(
        NOW - 3 * H,
        END,
        overrides=merge(
            rain(_local(9, 6), 2.0, 15),  # 9 Apr: rain all day (07:00–20:00 in the band)
            _wind(_local(10, 6), 10, 30.0),  # 10 Apr: open 16:00–20:00 only (4 h)
            # 11 Apr: open all band (14 h); 12 Apr the same, later so not best
            rain(_local(13, 6), 1.0, 7),  # 13 Apr: rain until 12:00, open from 12:00 (8 h)
        ),
    )

    days = rank_days(s, NOW, TZ, CONFIG)

    assert _grades(days) == [(9, "avoid"), (10, "good"), (11, "best"), (12, "good"), (13, "good")]
    assert sum(d.grade == "best" for d in days) == 1
    best = days[2]
    assert (best.open_from, best.open_until) == (_local(11, 6), _local(11, 20))
    assert best.reason == "Open 06:00–20:00 (14 h): no rain, wind up to 5 km/h"
    assert days[1].reason == "Open 16:00–20:00 (4 h): no rain, wind up to 5 km/h"
    assert days[0].reason == "Rain 28.0 mm"


def test_only_single_open_hours_is_possible_and_reasons_name_rain_and_wind():
    gaps = merge(*({_local(10, h): {"rr1_mm": 1.0}} for h in range(6, 22, 3)))
    s = series(NOW - 3 * H, END, overrides=merge(gaps, _wind(_local(11, 6), 14, 25.0)))

    days = {d.day.day: d for d in rank_days(s, NOW, TZ, CONFIG)}

    assert days[10].grade == "possible"
    assert days[10].reason == "Only short gaps (1 h open at most): rain 4.0 mm"
    assert days[11].grade == "avoid"
    assert days[11].reason == "Wind up to 25 km/h"


def test_days_beyond_the_forecast_are_not_ranked():
    s = series(NOW - 3 * H, _local(11, 12))

    days = rank_days(s, NOW, TZ, CONFIG)

    assert _grades(days) == [
        (9, "best"),
        (10, "good"),
        (11, "no_forecast"),
        (12, "no_forecast"),
        (13, "no_forecast"),
    ]
    assert days[2].reason == "Beyond the forecast"


def test_today_counts_only_the_hours_left_and_starts_tomorrow_after_the_band():
    s = series(NOW - 3 * H, END + 24 * H)

    midday = rank_days(s, _local(9, 15), TZ, CONFIG)
    evening = rank_days(s, _local(9, 21), TZ, CONFIG)

    assert midday[0].day.day == 9
    assert (midday[0].open_from, midday[0].open_until) == (_local(9, 15), _local(9, 20))
    assert [d.day.day for d in evening] == [10, 11, 12, 13, 14]


def test_sulphur_closes_hot_hours_only_when_sulphur_is_planned():
    hot = {_local(10, h): {"t_c": 33.0} for h in range(6, 18)}
    s = series(NOW - 3 * H, END, overrides=hot)

    copper = {d.day.day: d for d in rank_days(s, NOW, TZ, CONFIG)}
    sulphur = {d.day.day: d for d in rank_days(s, NOW, TZ, CONFIG, sulphur=True)}

    assert copper[10].open_until == _local(10, 20)
    assert (sulphur[10].open_from, sulphur[10].open_until) == (_local(10, 18), _local(10, 20))


def test_an_open_run_in_a_breeze_says_breezy_with_the_existing_12_kmh_flag():
    s = series(NOW - 3 * H, END, overrides=_wind(_local(10, 6), 14, 15.0))

    days = {d.day.day: d for d in rank_days(s, NOW, TZ, CONFIG)}

    assert days[10].grade == "good"  # 15 km/h is under the 19 km/h window limit
    assert days[10].reason == "Open 06:00–20:00 (14 h): no rain, breezy, wind up to 15 km/h"


def test_an_open_run_with_light_rain_shows_its_rain_total_not_dry():
    """The window lets through light rain (under 0.5 mm in the next 2 h): say how much."""
    s = series(NOW - 3 * H, END, overrides={_local(11, 10): {"rr1_mm": 0.2}})

    days = {d.day.day: d for d in rank_days(s, NOW, TZ, CONFIG)}

    assert days[11].open_hours == 14
    assert days[11].reason == "Open 06:00–20:00 (14 h): rain 0.2 mm, wind up to 5 km/h"


def test_equal_open_runs_go_to_less_rain_then_lower_wind_then_the_earliest_day():
    """An ordering only, no threshold: the window is weather-only."""
    s = series(
        NOW - 3 * H,
        END,
        overrides=merge(
            {_local(9, 10): {"rr1_mm": 0.3}},  # 9 Apr: 14 h open, a little rain
            _wind(_local(10, 6), 14, 15.0),  # 10 Apr: 14 h, dry, 15 km/h
            _wind(_local(11, 6), 14, 9.0),  # 11 Apr: 14 h, dry, 9 km/h
            _wind(_local(12, 6), 14, 9.0),  # 12 Apr: the same as 11 Apr, later
            _wind(_local(13, 6), 14, 9.0),
        ),
    )

    days = rank_days(s, NOW, TZ, CONFIG)

    assert [d.grade for d in days] == ["good", "good", "best", "good", "good"]
