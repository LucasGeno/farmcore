"""Spray window through `evaluate`, on an uncovered 3-10 event (8 Apr 09Z) at now = 9 Apr 12Z.

Open hour h: ff(h) ≤ 19 km/h, rr1(h) < 8 mm/h, sum rr1 over ts in (h, h + 2 h] < 0.5 mm
(and, for sulphur, t(h) < 32 °C). Open now = a run of ≥ 2 open hours starting at now.
"""

from datetime import timedelta

from farmcore.apps.advisor.engine import AdvisorConfig, evaluate
from farmcore.apps.advisor.engine.config import Threshold
from farmcore.apps.advisor.engine.window import spray_window
from tests.apps.advisor.engine.synth import at, merge, rain, series, utc

H = timedelta(hours=1)
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
NOW = utc(2024, 4, 9, 12)
EVENT = rain(utc(2024, 4, 8), 1.0, 12)


def _series(*overrides, end=NOW + 80 * H, **kw):
    return series(utc(2024, 4, 3), end, overrides=merge(EVENT, *overrides), **kw)


def _calm_except(hours, **fields):
    return merge(*(at(NOW + k * H, **fields) for k in hours))


def test_open_now_sprays_copper_in_the_current_window():
    advice = evaluate(_series(), [], CONFIG, NOW)

    assert advice.decision == "spray"
    assert advice.next_window[0] == NOW
    assert [p.name for p in advice.products_recommended] == ["copper"]
    window = advice.reason("spray_window")
    assert window.verdict == "open"
    assert window.inputs["rain_next_mm"] == 0.0


def test_wind_above_limit_closes_the_window_and_19_kmh_is_still_open():
    windy = evaluate(_series(_calm_except(range(0, 6), ff_kmh=20.0)), [], CONFIG, NOW)
    edge = evaluate(_series(_calm_except(range(0, 6), ff_kmh=19.0)), [], CONFIG, NOW)

    assert windy.decision == "wait"
    assert "wind" in windy.reason("spray_window").inputs["closed_by"]
    assert windy.next_window[0] == NOW + 6 * H
    assert edge.decision == "spray"


def test_heavy_rain_this_hour_closes_the_window():
    advice = evaluate(_series(at(NOW, rr1_mm=8.0)), [], CONFIG, NOW)

    assert advice.decision == "wait"
    assert "rain_rate" in advice.reason("spray_window").inputs["closed_by"]


def test_rain_in_the_next_two_hours_closes_the_window():
    advice = evaluate(_series(at(NOW + 1 * H, rr1_mm=0.5)), [], CONFIG, NOW)

    assert advice.decision == "wait"
    assert "rain_next" in advice.reason("spray_window").inputs["closed_by"]


def test_next_two_hours_boundary_0_2_mm_open_at_0_5_closed_at_0_2():
    s = _series(at(NOW + 2 * H, rr1_mm=0.2))
    strict = CONFIG.model_copy(
        update={"window_rain_next_max_mm": Threshold(value=0.2, source="test")}
    )

    lenient = evaluate(s, [], CONFIG, NOW)
    tight = evaluate(s, [], strict, NOW)

    assert lenient.reason("spray_window").inputs["rain_next_mm"] == 0.2
    assert lenient.decision == "spray"
    assert tight.decision == "wait"


def test_row_at_now_is_not_counted_in_next_two_hours():
    strict = CONFIG.model_copy(
        update={"window_rain_next_max_mm": Threshold(value=0.2, source="test")}
    )

    advice = evaluate(_series(at(NOW, rr1_mm=0.2)), [], strict, NOW)

    assert advice.reason("spray_window").inputs["rain_next_mm"] == 0.0
    assert advice.decision == "spray"


def test_next_window_is_the_first_run_of_at_least_two_open_hours():
    # Closed 0–5 h, a lone open hour at +6 (closed +7), then open +8..+11, closed +12.
    closed = _calm_except([0, 1, 2, 3, 4, 5, 7, 12], ff_kmh=30.0)

    advice = evaluate(_series(closed), [], CONFIG, NOW)

    assert advice.decision == "wait"
    assert advice.next_window == (NOW + 8 * H, NOW + 12 * H)
    assert "replay_forecast" in advice.flags


def test_no_window_within_72_hours():
    advice = evaluate(_series(ff=30.0), [], CONFIG, NOW)

    assert advice.decision == "wait"
    assert advice.next_window is None


def test_series_ending_at_now_has_no_forecast_and_no_replay_flag():
    advice = evaluate(_series(end=NOW), [], CONFIG, NOW)

    assert advice.decision == "wait"
    assert advice.next_window is None
    assert "replay_forecast" not in advice.flags


def test_sulphur_needs_temperature_below_32_c():
    hot = series(utc(2024, 4, 9), NOW + 80 * H, t=32.0)
    mild = series(utc(2024, 4, 9), NOW + 80 * H, t=31.9)

    assert not spray_window(hot, CONFIG, NOW, sulphur=True).open_now
    assert "sulphur_temp" in spray_window(hot, CONFIG, NOW, sulphur=True).reason.inputs["closed_by"]
    assert spray_window(hot, CONFIG, NOW, sulphur=False).open_now
    assert spray_window(mild, CONFIG, NOW, sulphur=True).open_now


def test_local_now_gives_utc_window_bounds():
    from datetime import UTC
    from zoneinfo import ZoneInfo

    advice = evaluate(_series(), [], CONFIG, NOW.astimezone(ZoneInfo("Europe/Paris")))

    assert advice.next_window[0] == NOW
    assert advice.next_window[0].tzinfo is UTC


def test_now_between_hours_gives_the_same_decision_as_on_the_hour():
    s = _series()
    on_hour = evaluate(s, [], CONFIG, NOW)

    for now in (NOW + 30 * timedelta(minutes=1), NOW + timedelta(seconds=7)):
        advice = evaluate(s, [], CONFIG, now)

        assert advice.decision == on_hour.decision == "spray"
        assert advice.next_window == on_hour.next_window
        assert advice.reason("spray_window").inputs["closed_by"] == []
        assert advice.reason("spray_window").inputs["rain_next_mm"] == 0.0


def test_now_between_hours_counts_rain_after_the_current_hour():
    # At 12:30 the next-2 h rain is the rows at 13:00 and 14:00, as at 12:00.
    advice = evaluate(_series(at(NOW + 2 * H, rr1_mm=0.5)), [], CONFIG, NOW + timedelta(minutes=30))

    assert advice.decision == "wait"
    assert "rain_next" in advice.reason("spray_window").inputs["closed_by"]


def test_window_never_ends_past_the_horizon():
    horizon = NOW + CONFIG.window_horizon_h.value * H

    advice = evaluate(_series(end=NOW + 80 * H), [], CONFIG, NOW)

    assert advice.next_window == (NOW, horizon)


def test_rows_through_forecast_span_reach_the_horizon():
    from farmcore.apps.advisor.engine.window import forecast_span

    horizon = NOW + CONFIG.window_horizon_h.value * H
    span = forecast_span(CONFIG)

    full = evaluate(_series(end=NOW + span), [], CONFIG, NOW)
    short = evaluate(_series(end=NOW + span - H), [], CONFIG, NOW)

    assert full.next_window == (NOW, horizon)
    assert short.next_window[1] < horizon
