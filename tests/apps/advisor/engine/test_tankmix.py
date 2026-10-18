"""Tank-mix: a due downy pass carries sulphur when the powdery cover ends within 2 days (or has
ended) in the powdery period — 1 pass, 2 treatments; sulphur stays out above 32 °C."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import AdvisorConfig, ProductRef, Spray, evaluate
from tests.apps.advisor.engine.synth import merge, rain, series

H = timedelta(hours=1)
D = timedelta(days=1)
PARIS = ZoneInfo("Europe/Paris")
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
BLOOM = CONFIG.powdery_start.value
SULPHUR = ProductRef(name="sulphur", product_type="contact", targets=("powdery",))
COPPER = ProductRef(name="copper", product_type="contact", targets=("downy",))


def _local(day, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=PARIS).astimezone(UTC)


IN_PERIOD = _local(BLOOM + 12 * D, 6)
BEFORE_PERIOD = _local(BLOOM - 10 * D, 6)


def _downy_due(now, *, t: float = 18.0, sulphur_days_ago: float | None = None, extra=None):
    """A 3-10 event 21 h before now (10 mm, not covered); flat T (18 °C: GT index 0 → 18 d
    sulphur interval); optionally a sulphur spray `sulphur_days_ago` days before now."""
    overrides = merge(rain(now - 30 * H, 1.0, 10), extra or {})
    rows = series(now - 40 * D, now + 80 * H, t=t, overrides=overrides)
    sprays = []
    if sulphur_days_ago is not None:
        sprays.append(Spray(ts=now - sulphur_days_ago * D, products=(SULPHUR,)))
    return evaluate(rows, sprays, CONFIG, now)


def _names(advice):
    return [p.name for p in advice.products_recommended]


def _sulphur_window(advice):
    found = [r for r in advice.reasons if r.rule == "spray_window" and r.inputs["sulphur"]]
    assert len(found) == 1
    return found[0]


def test_powdery_cover_ending_in_1_day_rides_the_downy_pass():
    advice = _downy_due(IN_PERIOD, sulphur_days_ago=17)  # 18 d interval → ends in 1 day

    assert advice.decision == "spray"
    assert _names(advice) == ["copper", "sulphur"]
    assert (advice.passes, advice.treatments) == (1, 2)
    assert advice.reason("tankmix").verdict == "tank-mix due: sulphur with copper"
    assert advice.reason("tankmix").inputs["powdery_cover_until"] == IN_PERIOD + D
    assert "copper + sulphur: 1 pass, 2 treatments" in _sulphur_window(advice).note


def test_powdery_cover_ended_or_never_applied_rides_the_downy_pass():
    for days_ago in (None, 25):
        advice = _downy_due(IN_PERIOD, sulphur_days_ago=days_ago)

        assert _names(advice) == ["copper", "sulphur"], days_ago
        assert (advice.passes, advice.treatments) == (1, 2)


def test_powdery_cover_holding_beyond_2_days_leaves_copper_alone():
    advice = _downy_due(IN_PERIOD, sulphur_days_ago=10)  # ends in 8 days

    assert advice.decision == "spray"
    assert _names(advice) == ["copper"]
    assert (advice.passes, advice.treatments) == (1, 1)
    assert advice.reason("tankmix").verdict == "copper only: powdery cover holds"


def test_outside_the_period_copper_goes_alone():
    advice = _downy_due(BEFORE_PERIOD, sulphur_days_ago=17)

    assert advice.decision == "spray"
    assert _names(advice) == ["copper"]
    assert (advice.passes, advice.treatments) == (1, 1)
    assert advice.reason("tankmix").verdict == "outside powdery period"


def test_at_32c_sulphur_is_blocked_and_copper_goes_alone():
    advice = _downy_due(IN_PERIOD, t=33.0, sulphur_days_ago=17)

    assert advice.decision == "spray"
    assert _names(advice) == ["copper"]
    assert (advice.passes, advice.treatments) == (1, 1)
    # The tankmix reason reads only the past; the window outcome sits on the sulphur window.
    assert advice.reason("tankmix").verdict == "tank-mix due: sulphur with copper"
    assert "2 treatments" not in advice.reason("tankmix").verdict
    sulphur = _sulphur_window(advice)
    assert "sulphur_temp" in sulphur.inputs["closed_by"]
    assert "copper alone: sulphur blocked: T ≥ 32 °C" in sulphur.note
    assert "copper + sulphur" not in sulphur.note


def test_copper_window_opening_later_at_32c_names_the_sulphur_limit():
    now = IN_PERIOD
    hot = {now + k * H: {"t_c": 33.0} for k in range(1, 81)}
    # Rain right after now closes the current hour (rain_next, at 18 °C) and the next 3 hours;
    # copper's window opens at now + 4 h, already above 32 °C.
    advice = _downy_due(now, extra=merge(rain(now + H, 1.0, 4), hot))

    assert advice.decision == "wait"
    assert advice.next_window[0] == now + 4 * H
    assert _names(advice) == ["copper"]
    assert (advice.passes, advice.treatments) == (1, 1)
    sulphur = _sulphur_window(advice)
    assert "sulphur_temp" not in sulphur.inputs["closed_by"]
    assert "copper alone: sulphur blocked: T ≥ 32 °C" in sulphur.note
    assert "33 °C" in sulphur.note


def test_powdery_pass_reason_does_not_claim_the_tank_mix_outcome():
    advice = _downy_due(IN_PERIOD, t=33.0, sulphur_days_ago=25)  # cover ended, sulphur blocked

    verdict = advice.reason("powdery_pass").verdict
    assert "with the downy pass" not in verdict
    assert "tankmix" in verdict
    assert advice.reason("powdery_pass").inputs["downy_protected_until"] is None


def test_new_event_under_a_holding_copper_cover_is_no_downy_pass():
    now = IN_PERIOD
    rows = series(now - 40 * D, now + 80 * H, t=18.0, overrides=rain(now - 30 * H, 1.0, 10))
    sprays = [
        Spray(ts=now - 3 * D, products=(COPPER,)),  # holds 7 d; 10 mm < 20 mm wash-off
        Spray(ts=now - 10 * D, products=(SULPHUR,)),  # holds the 18 d interval
    ]

    advice = evaluate(rows, sprays, CONFIG, now)

    assert advice.decision == "protected"
    assert advice.reason("tankmix").verdict == "no downy pass due"
    assert (advice.passes, advice.treatments) == (0, 0)


def test_tankmix_reason_echoes_its_threshold():
    reason = _downy_due(IN_PERIOD, sulphur_days_ago=17).reason("tankmix")

    assert reason.threshold["tankmix_cover_ending_days"] == 2
    assert reason.source
