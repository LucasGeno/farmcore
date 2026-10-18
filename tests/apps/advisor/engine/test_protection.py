"""Protection, expiry and wash-off through `evaluate`.

Config 2024: contact 8 d, penetrant 10 d, 7 d when sprayed in rapid growth (20 Apr – 30 Jun,
assumed); copper wash-off at ≥ 20 mm since the spray. Event = 3-10 crossing on 8 Apr 09Z from
1 mm/h rain starting 8 Apr 00Z.
"""

from datetime import timedelta

from farmcore.apps.advisor.engine import AdvisorConfig, ProductRef, Spray, evaluate
from tests.apps.advisor.engine.synth import merge, rain, series, utc

H = timedelta(hours=1)
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
COPPER = ProductRef(name="copper", product_type="contact", targets=("downy",))
SYSTEMIC = ProductRef(name="penetrant-x", product_type="penetrant", targets=("downy",))
SULPHUR = ProductRef(name="sulphur", product_type="contact", targets=("powdery",))


def _series(*overrides, end):
    return series(utc(2024, 3, 25), end + 80 * H, overrides=merge(*overrides))


def test_cover_after_the_event_ends_it_and_reads_protected():
    spray = Spray(ts=utc(2024, 4, 8, 12), products=(COPPER,))
    now = utc(2024, 4, 9, 12)
    s = _series(rain(utc(2024, 4, 8), 1.0, 12), end=now)

    advice = evaluate(s, [spray], CONFIG, now)

    assert advice.decision == "protected"
    assert advice.protected_until == utc(2024, 4, 16, 12)
    assert advice.mm_since_spray == 0.0
    assert advice.reason("downy_3_10").inputs["active_events"] == []
    assert advice.reason("protection_downy").verdict == "protected"


def test_cover_that_ended_an_event_does_not_revive_it_when_washed_off():
    spray = Spray(ts=utc(2024, 4, 8, 12), products=(COPPER,))
    now = utc(2024, 4, 9, 20)
    # 20 mm after the spray while R48 stays ≥ 10: no re-arm, so no new event.
    s = _series(rain(utc(2024, 4, 8), 1.0, 12), rain(utc(2024, 4, 9), 1.0, 20), end=now)

    advice = evaluate(s, [spray], CONFIG, now)

    assert advice.decision == "none"
    assert "washed_off" in advice.flags


def test_risk_and_valid_earlier_cover_is_protected():
    spray = Spray(ts=utc(2024, 4, 7, 12), products=(COPPER,))
    now = utc(2024, 4, 9, 12)

    advice = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 12), end=now), [spray], CONFIG, now)

    assert advice.decision == "protected"
    assert advice.mm_since_spray == 12.0
    assert advice.reason("downy_3_10").verdict == "risk"


def test_contact_expires_after_8_days_penetrant_after_10():
    sprayed = utc(2024, 3, 31, 12)
    now = utc(2024, 4, 9, 12)  # spray + 9 d
    s = _series(rain(utc(2024, 4, 8), 1.0, 12), end=now)

    contact = evaluate(s, [Spray(ts=sprayed, products=(COPPER,))], CONFIG, now)
    penetrant = evaluate(s, [Spray(ts=sprayed, products=(SYSTEMIC,))], CONFIG, now)

    assert contact.decision == "spray"
    assert "expired" in contact.flags
    assert contact.reason("protection_downy").inputs["until"] == utc(2024, 4, 8, 12)
    assert penetrant.decision == "protected"
    assert penetrant.protected_until == utc(2024, 4, 10, 12)
    assert "expired" not in penetrant.flags


def test_rapid_growth_phase_shortens_protection_to_7_days():
    sprayed = utc(2024, 4, 25, 12)
    now = utc(2024, 5, 1, 12)
    s = _series(rain(utc(2024, 4, 30), 1.0, 12), end=now)

    advice = evaluate(s, [Spray(ts=sprayed, products=(SYSTEMIC,))], CONFIG, now)

    assert advice.decision == "protected"
    assert advice.protected_until == utc(2024, 5, 2, 12)


def test_wash_off_at_20_mm_since_the_spray():
    sprayed = utc(2024, 4, 7, 12)
    now = utc(2024, 4, 9, 12)
    spray = Spray(ts=sprayed, products=(COPPER,))

    below = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 19), end=now), [spray], CONFIG, now)
    at_threshold = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 20), end=now), [spray], CONFIG, now)

    assert below.decision == "protected"
    assert below.mm_since_spray == 19.0
    assert at_threshold.decision == "spray"
    assert at_threshold.mm_since_spray == 20.0
    assert "washed_off" in at_threshold.flags
    assert "expired" not in at_threshold.flags
    assert at_threshold.reason("protection_downy").verdict == "washed off"


def test_rain_in_the_spray_hour_row_is_before_the_spray():
    # RR1 at the spray's own timestamp covers the hour before it.
    sprayed = utc(2024, 4, 8, 0)
    now = utc(2024, 4, 9, 12)
    s = _series(rain(utc(2024, 4, 7, 15), 1.0, 10), rain(utc(2024, 4, 8, 1), 1.0, 2), end=now)

    advice = evaluate(s, [Spray(ts=sprayed, products=(COPPER,))], CONFIG, now)

    assert advice.mm_since_spray == 2.0


def test_penetrant_is_not_washed_off():
    spray = Spray(ts=utc(2024, 4, 7, 12), products=(SYSTEMIC,))
    now = utc(2024, 4, 9, 12)

    advice = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 30), end=now), [spray], CONFIG, now)

    assert advice.decision == "protected"
    assert "washed_off" not in advice.flags


def test_flags_describe_the_latest_cover_only():
    old = Spray(ts=utc(2024, 3, 26, 12), products=(COPPER,))  # expired long ago
    latest = Spray(ts=utc(2024, 4, 7, 12), products=(COPPER,))
    now = utc(2024, 4, 9, 12)

    advice = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 12), end=now), [old, latest], CONFIG, now)

    assert advice.decision == "protected"
    assert "expired" not in advice.flags


def test_sprays_after_now_are_ignored():
    future = Spray(ts=utc(2024, 4, 9, 13), products=(COPPER,))
    now = utc(2024, 4, 9, 12)

    advice = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 12), end=now), [future], CONFIG, now)

    assert advice.decision == "spray"


def test_powdery_cover_is_tracked_separately_from_downy():
    spray = Spray(ts=utc(2024, 4, 8, 12), products=(SULPHUR,))
    now = utc(2024, 4, 9, 12)

    advice = evaluate(_series(rain(utc(2024, 4, 8), 1.0, 12), end=now), [spray], CONFIG, now)

    assert advice.decision == "spray"  # sulphur does not cover downy
    assert advice.reason("protection_powdery").verdict == "protected"
    assert advice.reason("protection_downy").verdict == "no cover"
