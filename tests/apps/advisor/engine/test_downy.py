"""3-10 rule on rolling 48 h hourly windows, through `evaluate`.

Gate 2024 = 5 Apr (local). Synthetic series default to 15 °C, dry, calm. A run of N rows at
1 mm/h first reaches R48 ≥ 10 on its 10th row; R48 falls below 10 once fewer than 10 rain rows
remain in the 48 rows ending at the hour.
"""

from farmcore.apps.advisor.engine import AdvisorConfig, evaluate
from tests.apps.advisor.engine.synth import HOUR, merge, rain, series, utc

CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)


def _downy(advice):
    return advice.reason("downy_3_10")


def test_event_fires_once_on_the_crossing_hour():
    s = series(utc(2024, 4, 3), utc(2024, 4, 9, 12), overrides=rain(utc(2024, 4, 8), 1.0, 12))

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 9, 12))

    downy = _downy(advice)
    assert downy.inputs["events"] == [utc(2024, 4, 8, 9)]
    assert downy.verdict == "risk"
    assert advice.decision in {"spray", "wait"}


def test_sustained_rain_above_threshold_is_one_event_not_one_per_hour():
    s = series(utc(2024, 4, 3), utc(2024, 4, 10, 23), overrides=rain(utc(2024, 4, 8), 1.0, 72))

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 10, 23))

    assert _downy(advice).inputs["events"] == [utc(2024, 4, 8, 9)]


def test_rearms_after_r48_drops_below_threshold():
    # Burst 1: 8 Apr 00–11Z; R48 < 10 from 10 Apr 02Z. Burst 2 from 11 Apr 00Z crosses at 09Z.
    overrides = merge(rain(utc(2024, 4, 8), 1.0, 12), rain(utc(2024, 4, 11), 1.0, 12))
    s = series(utc(2024, 4, 3), utc(2024, 4, 11, 12), overrides=overrides)

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 11, 12))

    assert _downy(advice).inputs["events"] == [utc(2024, 4, 8, 9), utc(2024, 4, 11, 9)]


def test_no_rearm_while_r48_stays_at_or_above_threshold():
    # Burst 2 starts while 11 of burst 1's rows are still inside the 48 h window.
    overrides = merge(rain(utc(2024, 4, 8), 1.0, 12), rain(utc(2024, 4, 9, 23), 1.0, 12))
    s = series(utc(2024, 4, 3), utc(2024, 4, 10, 12), overrides=overrides)

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 10, 12))

    assert _downy(advice).inputs["events"] == [utc(2024, 4, 8, 9)]


def test_cold_rain_is_no_event():
    s = series(
        utc(2024, 4, 3), utc(2024, 4, 9, 12), t=8.0, overrides=rain(utc(2024, 4, 8), 1.0, 12)
    )

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 9, 12))

    assert _downy(advice).inputs["events"] == []
    assert advice.decision == "none"


def test_event_is_active_risk_at_most_three_days():
    s = series(utc(2024, 4, 3), utc(2024, 4, 11, 10), overrides=rain(utc(2024, 4, 8), 1.0, 12))

    still = evaluate(s, [], CONFIG, utc(2024, 4, 11, 8))  # event + 2 d 23 h
    over = evaluate(s, [], CONFIG, utc(2024, 4, 11, 10))  # event + 3 d 1 h

    assert _downy(still).verdict == "risk"
    assert still.decision in {"spray", "wait"}
    assert _downy(over).verdict == "no risk"
    assert over.decision == "none"


def test_missing_rain_hours_are_skipped_not_zeroed_and_flag_data_gap():
    # 12 rain rows, two of them missing: only 10 mm counted, crossing on the last rain row.
    overrides = merge(
        rain(utc(2024, 4, 8), 1.0, 12),
        rain(utc(2024, 4, 8, 3), None, 2),
    )
    s = series(utc(2024, 4, 3), utc(2024, 4, 9, 12), overrides=overrides)

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 9, 12))

    assert _downy(advice).inputs["events"] == [utc(2024, 4, 8, 11)]
    assert "data_gap" in advice.flags


def test_complete_series_has_no_data_gap():
    s = series(utc(2024, 4, 3), utc(2024, 4, 9, 12), overrides=rain(utc(2024, 4, 8), 1.0, 12))

    assert "data_gap" not in evaluate(s, [], CONFIG, utc(2024, 4, 9, 12)).flags


def test_series_ending_hours_before_now_flags_data_gap():
    now = utc(2024, 4, 9, 12)
    s = series(utc(2024, 4, 3), now - 10 * HOUR, overrides=rain(utc(2024, 4, 8), 1.0, 12))

    advice = evaluate(s, [], CONFIG, now)

    assert "data_gap" in advice.flags
    assert _downy(advice).inputs["data_gap_hours"] == 10


def test_now_between_hours_with_the_current_hour_present_has_no_data_gap():
    s = series(utc(2024, 4, 3), utc(2024, 4, 9, 12), overrides=rain(utc(2024, 4, 8), 1.0, 12))

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 9, 12) + HOUR / 2)

    assert "data_gap" not in advice.flags
