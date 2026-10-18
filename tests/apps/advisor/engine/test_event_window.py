"""The 3-10 reason carries the infection event's own window, before the window at now.

Gate 2024 = 5 Apr (local). Synthetic series default to dry and calm; here 13 °C throughout.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig, ProductRef, Spray, evaluate
from farmcore.apps.advisor.engine.daily import evaluate_daily
from tests.apps.advisor.engine.daily_synth import days
from tests.apps.advisor.engine.synth import merge, rain, series, utc

CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
EVENT_KEYS = ["event_at", "event_r48_mm", "event_t48_c"]
COPPER = ProductRef(name="copper", product_type="contact", targets=("downy",))


def test_event_values_are_the_events_window_not_the_window_at_now():
    # 12 mm in one hour at 8 Apr 06Z (13 °C), then 48 dry hours: the window at now holds no rain.
    event = utc(2024, 4, 8, 6)
    now = utc(2024, 4, 10, 6)
    s = series(utc(2024, 4, 3), now, t=13.0, overrides=rain(event, 12.0, 1))

    advice = evaluate(s, [], CONFIG, now)

    downy = advice.reason("downy_3_10")
    assert downy.inputs["event_at"] == event
    assert downy.inputs["event_r48_mm"] == 12.0
    assert downy.inputs["event_t48_c"] == 13.0
    assert downy.inputs["event_r48_mm"] >= CONFIG.downy_rain_min_mm.value
    assert downy.inputs["event_t48_c"] >= CONFIG.downy_temp_min_c.value
    assert downy.inputs["r48_mm"] < CONFIG.downy_rain_min_mm.value
    assert downy.verdict == "risk"
    # Listed first, so the card's rule row shows them before the current-window values.
    assert list(downy.inputs)[:3] == EVENT_KEYS
    assert "r48_mm" in downy.inputs and "t48_c" in downy.inputs


def test_event_window_skips_missing_rows_and_uses_the_latest_active_event():
    # Two events in the 3-day lookback, 49 h apart (the rule re-arms between them); the latest
    # one's window has a row with missing rain and temperature.
    first, second = utc(2024, 4, 7, 7), utc(2024, 4, 9, 8)
    gap = utc(2024, 4, 9, 2)
    overrides = merge(
        rain(first, 11.0, 1),
        rain(second, 10.5, 1),
        {gap: {"rr1_mm": None, "t_c": None}},
        {utc(2024, 4, 9, 3): {"t_c": 16.0}},
    )
    s = series(utc(2024, 4, 3), utc(2024, 4, 10, 6), t=12.0, overrides=overrides)

    advice = evaluate(s, [], CONFIG, utc(2024, 4, 10, 6))

    downy = advice.reason("downy_3_10")
    assert downy.inputs["events"] == [first, second]
    assert downy.inputs["event_at"] == second
    assert downy.inputs["event_r48_mm"] == 10.5
    # 47 rows with a temperature: 46 at 12 °C and one at 16 °C (the missing row is not a 0).
    assert downy.inputs["event_t48_c"] == pytest.approx((46 * 12.0 + 16.0) / 47)


def test_a_covered_event_still_reports_its_window():
    # Event, then a copper spray: no active event, the latest recent one is reported.
    event = utc(2024, 4, 8, 6)
    s = series(utc(2024, 4, 3), utc(2024, 4, 10, 6), t=13.0, overrides=rain(event, 12.0, 1))
    sprays = [Spray(ts=utc(2024, 4, 8, 12), products=(COPPER,))]

    downy = evaluate(s, sprays, CONFIG, utc(2024, 4, 10, 6)).reason("downy_3_10")

    assert downy.verdict == "covered"
    assert downy.inputs["event_at"] == event
    assert downy.inputs["event_r48_mm"] == 12.0


def test_no_event_in_the_lookback_gives_none():
    now = utc(2024, 4, 10, 6)
    s = series(utc(2024, 4, 3), now, t=13.0)

    downy = evaluate(s, [], CONFIG, now).reason("downy_3_10")

    assert downy.verdict == "no risk"
    assert {k: downy.inputs[k] for k in EVENT_KEYS} == dict.fromkeys(EVENT_KEYS)


def test_daily_form_reports_the_events_two_calendar_days():
    # 6 + 6 mm on 14–15 Apr (mean 13 °C and 15 °C), dry since: the event ends 15 Apr (local).
    paris = ZoneInfo("Europe/Paris")
    rows = days(
        date(2024, 4, 1),
        date(2024, 4, 30),
        overrides={
            date(2024, 4, 14): {"precip_mm": 6.0, "tmean_c": 13.0},
            date(2024, 4, 15): {"precip_mm": 6.0},
        },
    )
    now = datetime(2024, 4, 18, 6, tzinfo=paris).astimezone(UTC)

    downy = evaluate_daily(rows, [], CONFIG, now).reason("downy_3_10")

    assert downy.verdict == "risk"
    assert list(downy.inputs)[:3] == EVENT_KEYS
    assert downy.inputs["event_at"] == datetime(2024, 4, 16, tzinfo=paris).astimezone(UTC)
    assert downy.inputs["event_r48_mm"] == 12.0
    assert downy.inputs["event_t48_c"] == 14.0
    assert downy.inputs["r48_mm"] == 0.0  # the 2 days ending at the latest row are dry
