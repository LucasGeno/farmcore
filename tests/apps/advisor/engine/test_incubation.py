"""Goidanich incubation (agronomy.md §2b): table interpolation, the low-T ramp, hourly progress."""

from datetime import timedelta

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig, evaluate
from farmcore.apps.advisor.engine.incubation import daily_rate
from tests.apps.advisor.engine.synth import merge, rain, series, utc

H = timedelta(hours=1)
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)


def test_rate_interpolates_linearly_between_table_rows():
    # Table: 12.0 humid 5.25, 12.5 humid 6.2 → midpoint 5.725.
    assert daily_rate(12.25, 95.0, CONFIG) == pytest.approx(5.725)
    assert daily_rate(16.5, 50.0, CONFIG) == pytest.approx(9.3)  # corrected typo cell
    assert daily_rate(20.0, 95.0, CONFIG) == pytest.approx(20.0)


def test_rate_is_zero_at_or_below_incub_tmin():
    assert daily_rate(11.0, 95.0, CONFIG) == 0.0
    assert daily_rate(8.0, 99.0, CONFIG) == 0.0


def test_ramp_between_incub_tmin_and_the_12_degree_row():
    assert daily_rate(11.5, 95.0, CONFIG) == pytest.approx(2.625)  # half of 5.25
    assert daily_rate(11.5, 50.0, CONFIG) == 0.0  # dry 12.0 cell is 0 (assumed)


def test_humid_column_from_hourly_rh_threshold():
    assert daily_rate(20.0, 89.9, CONFIG) == pytest.approx(14.2)
    assert daily_rate(20.0, 90.0, CONFIG) == pytest.approx(20.0)


def test_above_25_degrees_rate_stays_at_the_25_row():
    assert daily_rate(30.0, 95.0, CONFIG) == pytest.approx(22.2)
    assert daily_rate(23.5, 50.0, CONFIG) == pytest.approx(18.1)


# --- Hourly accumulation per 3-10 event, through `evaluate` ---------------------------------

START = utc(2024, 4, 10)
RAIN_AT = utc(2024, 4, 12)
EVENT = RAIN_AT + 9 * H  # 10 × 1 mm/h: R48 reaches 10 mm on the 10th hour


def _warm_humid(end):
    return series(START, end, t=20.0, rh=95.0, overrides=rain(RAIN_AT, 1.0, 10))


def _incubation(now, end=None):
    advice = evaluate(_warm_humid(end or now), [], CONFIG, now)
    return advice.reason("downy_incubation")


def test_constant_20c_humid_reaches_50_pct_at_2_5_days():
    reason = _incubation(EVENT + 60 * H)

    assert reason.inputs["event"] == EVENT
    assert reason.inputs["pct"] == pytest.approx(50.0)
    assert reason.inputs["alert_at"] == EVENT + 60 * H
    assert reason.inputs["symptoms_at"] is None
    assert reason.verdict == "alert"
    # Projected from the mean rate so far (20 %/day): 2.5 more days.
    assert reason.inputs["expected_symptoms"] == EVENT + 120 * H
    assert reason.inputs["projected"] is True


def test_constant_20c_humid_reaches_100_pct_after_5_days():
    reason = _incubation(EVENT + 5 * 24 * H)

    assert reason.inputs["pct"] == pytest.approx(100.0)
    assert reason.inputs["symptoms_at"] == EVENT + 120 * H
    assert reason.inputs["expected_symptoms"] == EVENT + 120 * H
    assert reason.inputs["projected"] is False
    assert reason.verdict == "symptoms expected"


def test_progress_is_capped_at_100_pct():
    reason = _incubation(EVENT + 8 * 24 * H)

    assert reason.inputs["pct"] == pytest.approx(100.0)
    assert reason.inputs["symptoms_at"] == EVENT + 120 * H


def test_before_alert_the_card_reads_incubating_and_is_a_risk_indicator():
    reason = _incubation(EVENT + 24 * H)

    assert reason.inputs["pct"] == pytest.approx(20.0)
    assert reason.verdict == "incubating"
    assert "risk indicator" in reason.note
    assert reason.threshold == {
        "incub_tmin": 11.0,
        "incub_humid_rh_hourly": 90.0,
        "incub_alert_pct": 50.0,
        "incub_symptoms_pct": 100.0,
    }
    assert "Goidanich" in reason.source


def test_no_event_means_no_incubation():
    advice = evaluate(series(START, utc(2024, 4, 20)), [], CONFIG, utc(2024, 4, 20))

    reason = advice.reason("downy_incubation")
    assert reason.verdict == "no event"
    assert reason.inputs["event"] is None


def test_hours_after_now_do_not_move_incubation():
    now = EVENT + 30 * H

    assert _incubation(now, end=now + 100 * H).inputs == _incubation(now).inputs


def test_missing_temperature_hours_add_nothing_and_are_counted():
    now = EVENT + 24 * H
    gaps = {EVENT + k * H: {"t_c": None} for k in (1, 2, 3)}
    s = series(START, now, t=20.0, rh=95.0, overrides=merge(rain(RAIN_AT, 1.0, 10), gaps))

    reason = evaluate(s, [], CONFIG, now).reason("downy_incubation")

    assert reason.inputs["pct"] == pytest.approx(20.0 * 21 / 24)
    assert reason.inputs["missing_hours"] == 3


def test_each_event_accumulates_from_its_own_start():
    second_rain = EVENT + 100 * H  # R48 re-armed long before
    second = second_rain + 9 * H
    now = second + 24 * H
    s = series(
        START,
        now,
        t=20.0,
        rh=95.0,
        overrides=merge(rain(RAIN_AT, 1.0, 10), rain(second_rain, 1.0, 10)),
    )

    reason = evaluate(s, [], CONFIG, now).reason("downy_incubation")

    assert reason.inputs["event"] == second
    assert reason.inputs["pct"] == pytest.approx(20.0)
    first = next(e for e in reason.inputs["events"] if e["event"] == EVENT)
    assert first["pct"] == pytest.approx(100.0)
