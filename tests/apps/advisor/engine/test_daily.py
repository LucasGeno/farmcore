"""Daily engine form through `evaluate_daily`: 3-10 on 2 calendar days, the daily
window, Gubler–Thomas on synthetic hourly T, daily Goidanich. Invented daily values only."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig, ProductRef, Spray, evaluate
from farmcore.apps.advisor.engine.config import Threshold
from farmcore.apps.advisor.engine.daily import evaluate_daily, synthetic_hourly
from tests.apps.advisor.engine.daily_synth import days
from tests.apps.advisor.engine.synth import rain, series, utc

PARIS = ZoneInfo("Europe/Paris")
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)  # gate 5 Apr, bloom 28 May


def _at6(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 6, tzinfo=PARIS).astimezone(UTC)


def _end_of(d: date) -> datetime:
    """A daily event is dated at the end of its day (local midnight)."""
    return datetime.combine(d + timedelta(days=1), time(0), tzinfo=PARIS).astimezone(UTC)


def test_3_10_sums_rain_over_two_calendar_days():
    series = days(
        date(2024, 4, 1),
        date(2024, 4, 30),
        overrides={date(2024, 4, 14): {"precip_mm": 6.0}, date(2024, 4, 15): {"precip_mm": 4.0}},
    )

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 4, 16)))

    downy = advice.reason("downy_3_10")
    assert downy.verdict == "risk"
    assert downy.inputs["events"] == [_end_of(date(2024, 4, 15))]
    assert downy.threshold["daily.downy_days"] == 2
    assert "2-calendar-day approximation" in downy.note
    assert advice.decision == "spray"


def test_3_10_needs_10_mm_in_two_days_and_ignores_a_third():
    series = days(
        date(2024, 4, 1),
        date(2024, 4, 30),
        overrides={
            date(2024, 4, 13): {"precip_mm": 5.0},
            date(2024, 4, 14): {"precip_mm": 0.0},
            date(2024, 4, 15): {"precip_mm": 9.9},
        },
    )

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 4, 16)))

    assert advice.reason("downy_3_10").inputs["events"] == []
    assert advice.decision == "none"


def test_3_10_needs_two_day_mean_t_of_10():
    cold = {"precip_mm": 6.0, "tmean_c": 9.0}
    series = days(
        date(2024, 4, 1),
        date(2024, 4, 30),
        overrides={date(2024, 4, 14): cold, date(2024, 4, 15): {**cold, "tmean_c": 10.9}},
    )  # mean of the two days 9.95 °C

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 4, 16)))

    assert advice.reason("downy_3_10").inputs["events"] == []


def test_3_10_is_edge_triggered_and_re_arms_below_10_mm():
    wet = {"precip_mm": 6.0}
    series = days(
        date(2024, 4, 1),
        date(2024, 4, 30),
        overrides={
            date(2024, 4, 10): wet,
            date(2024, 4, 11): wet,  # event (12 mm)
            date(2024, 4, 12): wet,  # still ≥ 10 mm over 11–12: same event
            # 13–14 dry: 2-day sum falls below 10, re-arms
            date(2024, 4, 15): wet,
            date(2024, 4, 16): wet,  # new event
        },
    )

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 4, 18)))

    assert advice.reason("downy_3_10").inputs["events"] == [
        _end_of(date(2024, 4, 11)),
        _end_of(date(2024, 4, 16)),
    ]


EVENT = {date(2024, 4, 14): {"precip_mm": 6.0}, date(2024, 4, 15): {"precip_mm": 4.0}}
TODAY = date(2024, 4, 16)  # evaluated at 06:00 local; the event (end of 15 Apr) is uncovered


def _advice_with(today: dict, tomorrow: dict | None = None):
    overrides = {**EVENT, TODAY: today, date(2024, 4, 17): tomorrow or {}}
    series = days(date(2024, 4, 1), date(2024, 4, 30), overrides=overrides)
    return evaluate_daily(series, [], CONFIG, _at6(TODAY))


@pytest.mark.parametrize(
    ("today", "tomorrow", "decision", "closed_by"),
    [
        ({"wind_ms": 5.3}, None, "spray", []),
        ({"wind_ms": 5.4}, None, "wait", ["wind"]),
        ({"precip_mm": 1.9}, None, "spray", []),
        ({"precip_mm": 2.0}, None, "wait", ["rain"]),
        ({}, {"precip_mm": 4.9}, "spray", []),
        ({}, {"precip_mm": 5.0}, "wait", ["rain_next"]),
        ({"wind_ms": None}, None, "wait", ["wind"]),
    ],
)
def test_daily_window_conditions(today, tomorrow, decision, closed_by):
    advice = _advice_with(today, tomorrow)

    assert advice.decision == decision
    win = advice.reason("spray_window_daily")
    assert win.inputs["closed_by"] == closed_by
    assert win.threshold["daily.window_wind_max_ms"] == 5.3
    assert win.threshold["daily.window_rain_max_mm"] == 2.0
    assert win.threshold["daily.window_rain_next_max_mm"] == 5.0
    assert "daily regional data" in win.note


def test_a_copper_spray_after_the_event_protects_with_daily_wash_off():
    copper = ProductRef(name="copper", product_type="contact", targets=("downy",))
    spray = Spray(ts=_at6(date(2024, 4, 16)), products=(copper,))
    series = days(
        date(2024, 4, 1),
        date(2024, 4, 30),
        overrides={**EVENT, date(2024, 4, 16): {"precip_mm": 12.0}},  # rain on the spray day
    )

    advice = evaluate_daily(series, [spray], CONFIG, _at6(date(2024, 4, 18)))

    assert advice.decision == "protected"
    assert advice.mm_since_spray == pytest.approx(12.0)  # the spray day's rain counts
    assert advice.reason("downy_3_10").verdict == "covered"


def test_daily_wait_names_the_next_open_day():
    advice = _advice_with({"wind_ms": 8.0})

    assert advice.decision == "wait"
    # 17 Apr is open: the window opens at 06:00 local and runs to the end of the day.
    assert advice.next_window == (_at6(date(2024, 4, 17)), _end_of(date(2024, 4, 17)))
    assert "replay_forecast" in advice.flags


def test_synthetic_hourly_t_hits_tmin_at_06_and_tmax_at_15_local():
    (day,) = days(date(2024, 6, 3), date(2024, 6, 3), tmin=12.0, tmax=28.0)

    rows = synthetic_hourly([day], CONFIG)

    by_local_hour = {o.ts.astimezone(PARIS).hour: o.t_c for o in rows}
    assert len(rows) == 24
    assert by_local_hour[6] == pytest.approx(12.0)
    assert by_local_hour[15] == pytest.approx(28.0)
    assert all(12.0 <= t <= 28.0 for t in by_local_hour.values())
    assert by_local_hour[3] > by_local_hour[6] < by_local_hour[9] < by_local_hour[15]
    assert all(o.rr1_mm is None and o.rh_pct is None for o in rows)  # T only


BLOOM = date(2024, 5, 28)  # powdery period starts (2024 config)
HOT = {"tmin_c": 18.0, "tmax_c": 30.0}  # sinusoid: ≥ 7 continuous hours in 21–29.5 °C
COOL = {"tmin_c": 10.0, "tmax_c": 20.0}  # never reaches 21 °C


def _powdery_season(day_fields: dict, n_days: int, **today):
    overrides = {date(2024, 5, 28 + i): day_fields for i in range(n_days)}
    overrides[date(2024, 5, 28 + n_days)] = today
    return days(date(2024, 4, 1), date(2024, 6, 10), overrides=overrides)


def test_gubler_thomas_runs_on_synthetic_hourly_t_from_tmin_tmax():
    hot = evaluate_daily(_powdery_season(HOT, 3), [], CONFIG, _at6(date(2024, 5, 31)))
    cool = evaluate_daily(_powdery_season(COOL, 3), [], CONFIG, _at6(date(2024, 5, 31)))

    gt = hot.reason("powdery_gubler_thomas")
    assert gt.inputs["index"] == 60  # three qualifying days, +20 each (UC IPM)
    assert gt.verdict == "high pressure"
    assert "derived" in gt.note
    assert cool.reason("powdery_gubler_thomas").inputs["index"] == 0


@pytest.mark.parametrize(("tmax", "decision"), [(31.9, "spray"), (32.0, "wait")])
def test_sulphur_only_pass_in_the_period_and_its_32_c_block(tmax, decision):
    series = _powdery_season(COOL, 1, tmax_c=tmax)  # today 29 May, no downy event

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 5, 29)))

    assert advice.decision == decision
    assert [p.name for p in advice.products_recommended] == ["sulphur"]
    assert (advice.passes, advice.treatments) == (1, 1)
    win = advice.reason("spray_window_daily")
    assert win.inputs["sulphur"] is True
    assert ("sulphur_temp" in win.inputs["closed_by"]) is (decision == "wait")


def test_downy_pass_in_the_period_with_no_powdery_cover_is_a_tank_mix():
    rain = {"precip_mm": 6.0}
    series = days(
        date(2024, 4, 1),
        date(2024, 6, 10),
        overrides={date(2024, 5, 29): rain, date(2024, 5, 30): rain},
    )

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 5, 31)))

    assert advice.decision == "spray"
    assert [p.name for p in advice.products_recommended] == ["copper", "sulphur"]
    assert (advice.passes, advice.treatments) == (1, 2)
    assert advice.reason("tankmix").verdict == "tank-mix due: sulphur with copper"


def _with_daily_split(value: float) -> AdvisorConfig:
    return CONFIG.model_copy(
        update={
            "daily": CONFIG.daily.model_copy(
                update={"incub_daily_rh_split": Threshold(value=value, source="test split")}
            )
        }
    )


def _incubation_after_one_day(rh: float, config: AdvisorConfig = CONFIG) -> float:
    # Event at the end of 15 Apr; 16 Apr at a daily mean of 15 °C is the one day counted.
    overrides = {**EVENT, date(2024, 4, 16): {"tmean_c": 15.0, "rh_pct": rh}}
    series_ = days(date(2024, 4, 1), date(2024, 4, 30), overrides=overrides)
    advice = evaluate_daily(series_, [], config, _at6(date(2024, 4, 17)))
    reason = advice.reason("downy_incubation")
    assert reason.inputs["event"] == _end_of(date(2024, 4, 15))
    return reason.inputs["pct"]


def test_daily_goidanich_splits_columns_at_daily_mean_rh_75():
    # Goidanich table at 15.0 °C: dry 7.6 %/day, humid 10.6 %/day (config incub_table).
    assert _incubation_after_one_day(74.0) == pytest.approx(7.6)
    assert _incubation_after_one_day(75.0) == pytest.approx(10.6)


def test_changing_the_daily_split_moves_it_and_leaves_the_hourly_key_alone():
    moved = _with_daily_split(80.0)

    assert _incubation_after_one_day(75.0, moved) == pytest.approx(7.6)
    assert _incubation_after_one_day(80.0, moved) == pytest.approx(10.6)
    assert moved.incub_humid_rh_hourly == CONFIG.incub_humid_rh_hourly
    assert moved.incub_humid_rh_hourly.value == 90.0
    # The hourly engine still picks its column at RH ≥ 90 %: event at 15 Apr 04Z (R48 hits
    # 10 mm), then 24 h at 15 °C and RH 80 → the dry column, 7.6 %, whatever the daily split.
    hourly = series(
        utc(2024, 4, 1), utc(2024, 4, 18), t=15.0, rh=80.0, overrides=rain(utc(2024, 4, 15), 2.0, 5)
    )
    for config in (CONFIG, _with_daily_split(70.0)):
        inc = evaluate(hourly, [], config, utc(2024, 4, 16, 4)).reason("downy_incubation")
        assert inc.inputs["event"] == utc(2024, 4, 15, 4)
        assert inc.inputs["pct"] == pytest.approx(7.6)


def test_a_daily_feed_that_stops_before_now_raises_data_gap():
    # Feed stops 15 Apr; evaluated 20 Apr 06:00. 17–19 Apr (complete days in the 3-day
    # lookback) have no row, so a stopped feed never reads as "no risk" without a warning.
    stopped = days(date(2024, 4, 1), date(2024, 4, 15))

    advice = evaluate_daily(stopped, [], CONFIG, _at6(date(2024, 4, 20)))

    assert "data_gap" in advice.flags
    assert advice.reason("downy_3_10").inputs["data_gap_days"] == 3


def test_a_complete_daily_feed_raises_no_data_gap():
    full = days(date(2024, 4, 1), date(2024, 4, 30))

    advice = evaluate_daily(full, [], CONFIG, _at6(date(2024, 4, 20)))

    assert "data_gap" not in advice.flags
    assert advice.reason("downy_3_10").inputs["data_gap_days"] == 0


# Reason parity with the hourly rules (powdery.rule, tankmix.rule, decision's downy_3_10).


def test_downy_reason_uses_the_hourly_input_names():
    series = days(date(2024, 4, 1), date(2024, 4, 30), overrides=EVENT)

    downy = evaluate_daily(series, [], CONFIG, _at6(date(2024, 4, 16))).reason("downy_3_10")

    assert downy.inputs["r48_mm"] == pytest.approx(10.0)  # 2 calendar days, as R48
    assert downy.inputs["t48_c"] == pytest.approx(15.0)
    assert "rain_mm" not in downy.inputs and "tmean_c" not in downy.inputs


@pytest.mark.parametrize("today", [date(2024, 4, 20), date(2024, 5, 29)])
def test_no_downy_pass_due_still_gives_the_tankmix_reason(today):
    series = days(date(2024, 4, 1), date(2024, 6, 10))  # no rain at all

    advice = evaluate_daily(series, [], CONFIG, _at6(today))

    assert advice.reason("tankmix").verdict == "no downy pass due"


def test_a_downy_pass_due_in_the_period_still_gives_the_powdery_pass_reason():
    wet = {"precip_mm": 6.0}
    series = days(
        date(2024, 4, 1),
        date(2024, 6, 10),
        overrides={date(2024, 5, 29): wet, date(2024, 5, 30): wet},
    )

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 5, 31)))

    passed = advice.reason("powdery_pass")
    assert passed.verdict == "sulphur due; no sulphur-only pass, tankmix decides (downy pass due)"
    assert passed.inputs["downy_due"] is True


def test_sulphur_only_wait_names_the_32_c_block():
    series = _powdery_season(COOL, 1, tmax_c=33.0)

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 5, 29)))

    assert advice.decision == "wait"
    assert advice.reason("spray_window_daily").note.startswith(
        "sulphur blocked: T ≥ 32 °C (window_sulphur_tmax_c)"
    )


def test_copper_alone_names_the_hot_day_that_blocks_sulphur():
    wet = {"precip_mm": 6.0}
    series = days(
        date(2024, 4, 1),
        date(2024, 6, 10),
        overrides={
            date(2024, 5, 29): wet,
            date(2024, 5, 30): wet,
            date(2024, 5, 31): {"tmax_c": 33.0},  # copper's day: open, but too hot for sulphur
        },
    )

    advice = evaluate_daily(series, [], CONFIG, _at6(date(2024, 5, 31)))

    assert [p.name for p in advice.products_recommended] == ["copper"]
    windows = [r for r in advice.reasons if r.rule == "spray_window_daily"]
    sulphur = next(r for r in windows if r.inputs["sulphur"])
    assert sulphur.note.startswith(
        "copper alone: sulphur blocked: T ≥ 32 °C (window_sulphur_tmax_c), Tmax 33 °C on "
        "2024-05-31 in copper's window"
    )


def _bumped(value):
    return value + timedelta(days=1) if isinstance(value, date) else value + 1


def _changed_everywhere() -> AdvisorConfig:
    """Every threshold, top-level and nested (calendar.*, daily.*), moved and re-sourced."""

    def moved(block, prefix: str = "") -> dict:
        return {
            name: Threshold(value=_bumped(th.value), source=f"test {prefix}{name}")
            for name in type(block).model_fields
            if isinstance(th := getattr(block, name), Threshold)
        }

    return CONFIG.model_copy(
        update={
            **moved(CONFIG),
            "calendar": CONFIG.calendar.model_copy(update=moved(CONFIG.calendar, "calendar.")),
            "daily": CONFIG.daily.model_copy(update=moved(CONFIG.daily, "daily.")),
        }
    )


def _assert_echo(threshold: dict, source: str, changed: AdvisorConfig, where: str) -> set[str]:
    for name, value in threshold.items():
        assert value == changed.threshold(name).value, (where, name)
        assert value != CONFIG.threshold(name).value, (where, name)
        assert f"test {name}" in source, (where, name)
    return set(threshold)


def test_every_daily_and_calendar_threshold_is_echoed_with_its_changed_value():
    from farmcore.apps.advisor.engine.simulate import calendar_passes, simulate
    from tests.apps.advisor.engine.synth import series as hourly_series

    changed = _changed_everywhere()
    assert set(AdvisorConfig.nested_threshold_names()) >= {
        "daily.incub_daily_rh_split",
        "calendar.downy_interval_days",
    }
    # Changed bloom is 29 May; 12 mm over 30–31 May (≥ 11 mm), copper and sulphur both due.
    wet = {"precip_mm": 6.0}
    daily_series = days(
        date(2024, 4, 1),
        date(2024, 6, 10),
        overrides={date(2024, 5, 30): wet, date(2024, 5, 31): wet},
    )

    advice = evaluate_daily(daily_series, [], changed, _at6(date(2024, 6, 1)))

    rules = {r.rule for r in advice.reasons}
    assert rules >= {
        "downy_3_10",
        "spray_window_daily",
        "downy_incubation",
        "powdery_gubler_thomas",
        "powdery_pass",
        "tankmix",
    }
    echoed: set[str] = set()
    for reason in advice.reasons:
        echoed |= _assert_echo(reason.threshold, reason.source, changed, reason.rule)
    cal = calendar_passes(changed, 2024)
    echoed |= _assert_echo(cal.threshold, cal.source, changed, "calendar_passes")
    hourly = simulate(
        hourly_series(utc(2024, 4, 1), utc(2024, 4, 12)),
        changed,
        date(2024, 4, 8),
        date(2024, 4, 9),
    )
    echoed |= _assert_echo(hourly.threshold, hourly.source, changed, "simulate")
    nested = {n for n in echoed if "." in n}
    assert nested == set(AdvisorConfig.nested_threshold_names())
