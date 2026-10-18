"""Gubler–Thomas powdery index (agronomy.md §2e) on local days between bloom and bunch closure,
and the sulphur interval it sets."""

import random
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs, ProductRef, Spray, evaluate
from farmcore.apps.advisor.engine.powdery import gubler_thomas, sulphur_interval_days
from tests.apps.advisor.engine.synth import rain, series

H = timedelta(hours=1)
D = timedelta(days=1)
PARIS = ZoneInfo("Europe/Paris")
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
COPPER = ProductRef(name="copper", product_type="contact", targets=("downy",))
SULPHUR = ProductRef(name="sulphur", product_type="contact", targets=("powdery",))
BLOOM = CONFIG.powdery_start.value

# Day kinds: Q = qualifies (6 continuous h at 25 °C), F = fails (5 h only), and a trailing "h"
# adds one hour at 36 °C (hot) outside the band run.
KINDS = {"Q": (6, False), "Qh": (6, True), "F": (5, False), "Fh": (5, True)}


def _local(day, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=PARIS).astimezone(UTC)


def _season(kinds: list[str], *, first=BLOOM, extra_hours: int = 6):
    """Hourly rows from bloom − 1 day: 18 °C baseline, then one local day per kind from `first`;
    returns (rows, now) with now = 06:00 local on the day after the last kind (+ extra rows)."""
    overrides: dict[datetime, dict] = {}
    for i, kind in enumerate(kinds):
        day = first + i * D
        band_hours, hot = KINDS[kind]
        for h in range(10, 10 + band_hours):
            overrides[_local(day, h)] = {"t_c": 25.0}
        if hot:
            overrides[_local(day, 17)] = {"t_c": 36.0}
    now = _local(first + len(kinds) * D, 6)
    rows = series(_local(BLOOM - D, 0), now + extra_hours * H, t=18.0, overrides=overrides)
    return rows, now


def _index(kinds: list[str]) -> int:
    rows, now = _season(kinds)
    return gubler_thomas(rows, CONFIG, now).index


def test_three_qualifying_days_reach_60():
    assert _index(["Q", "Q", "Q"]) == 60


def test_before_60_a_failing_day_resets_to_zero():
    assert _index(["Q", "Q", "F"]) == 0
    assert _index(["Q", "Q", "F", "Q"]) == 20
    assert _index(["Q", "Qh"]) == 40  # a hot hour does not matter before 60


def test_after_60_qualifying_without_a_35c_hour_adds_20():
    assert _index(["Q", "Q", "Q", "Q"]) == 80


def test_after_60_qualifying_with_a_35c_hour_adds_10():
    assert _index(["Q", "Q", "Q", "Qh"]) == 70


def test_after_60_non_qualifying_without_a_35c_hour_subtracts_10():
    assert _index(["Q", "Q", "Q", "F"]) == 50


def test_after_60_non_qualifying_with_a_35c_hour_subtracts_20():
    assert _index(["Q", "Q", "Q", "Fh"]) == 40


def test_after_onset_the_index_never_resets():
    assert _index(["Q", "Q", "Q", "F", "F", "Q"]) == 60


def test_index_is_bounded_0_to_100():
    assert _index(["Q"] * 8) == 100
    assert _index(["Q", "Q", "Q"] + ["Fh"] * 5) == 0
    assert _index(["Q", "Q", "Q"] + ["Fh"] * 5 + ["Q"]) == 20


@pytest.mark.parametrize(
    ("index", "days"),
    [(100, 7), (60, 7), (55, 7), (50, 14), (40, 14), (35, 14), (30, 18), (10, 18), (0, 18)],
)
def test_interval_mapping_with_gaps_mapped_up(index, days):
    assert sulphur_interval_days(index, CONFIG) == days


def test_days_before_bloom_do_not_count():
    rows, now = _season(["Q", "Q", "Q", "Q"], first=BLOOM - 2 * D)

    assert gubler_thomas(rows, CONFIG, now).index == 40


def test_hours_after_now_and_the_current_day_are_ignored():
    rows, now = _season(["Q", "Q"])
    # Same days, plus qualifying runs later today and tomorrow (after now).
    longer, _ = _season(["Q", "Q", "Q", "Q"])

    assert gubler_thomas(longer, CONFIG, now) == gubler_thomas(rows, CONFIG, now)
    assert gubler_thomas(rows, CONFIG, now).index == 40


def test_reason_names_index_interval_and_period():
    rows, now = _season(["Q", "Q", "Q"])

    advice = evaluate(rows, [], CONFIG, now)

    reason = advice.reason("powdery_gubler_thomas")
    assert reason.inputs["index"] == 60
    assert reason.inputs["interval_days"] == 7
    assert reason.inputs["in_period"] is True
    assert reason.threshold["gt_min_hours"] == 6
    assert reason.threshold["powdery_start"] == BLOOM
    assert "UC IPM" in reason.source


def test_powdery_cover_in_the_period_lasts_the_gubler_thomas_interval():
    # Flat 18 °C: no qualifying day, index 0 → low pressure, 18 d sulphur interval.
    now = _local(BLOOM + 12 * D, 6)
    sprayed = now - 10 * D
    rows = series(_local(BLOOM - 20 * D, 0), now + 6 * H, t=18.0)

    advice = evaluate(rows, [Spray(ts=sprayed, products=(SULPHUR,))], CONFIG, now)

    assert advice.decision == "none"
    cover = advice.reason("protection_powdery")
    assert cover.verdict == "protected"
    assert cover.inputs["until"] == sprayed + 18 * D
    assert cover.inputs["interval_days"] == 18
    assert "expired" not in advice.flags


# --- Sulphur-only pass ----------------------------------------------------------------------


def _flat(now, t: float):
    return series(_local(BLOOM - 20 * D, 0), now + 80 * H, t=t)


def test_sulphur_only_pass_when_never_applied_and_window_open():
    now = _local(BLOOM + 5 * D, 6)

    advice = evaluate(_flat(now, 25.0), [], CONFIG, now)

    assert advice.decision == "spray"
    assert [p.name for p in advice.products_recommended] == ["sulphur"]
    assert (advice.passes, advice.treatments) == (1, 1)
    assert advice.reason("downy_3_10").verdict == "no risk"
    assert advice.reason("powdery_pass").verdict == "sulphur due"
    assert advice.reason("spray_window").inputs["sulphur"] is True
    assert advice.next_window[0] == now.replace(minute=0)


def test_sulphur_only_pass_waits_at_32c_and_names_the_limit():
    now = _local(BLOOM + 5 * D, 6)

    advice = evaluate(_flat(now, 33.0), [], CONFIG, now)

    assert advice.decision == "wait"
    assert [p.name for p in advice.products_recommended] == ["sulphur"]
    window = advice.reason("spray_window")
    assert "sulphur_temp" in window.inputs["closed_by"]
    assert "32" in window.note and "sulphur" in window.note
    assert advice.next_window is None


def test_sulphur_due_again_once_the_high_pressure_interval_ends():
    # Flat 25 °C: every day qualifies → index ≥ 60 from day 3 → 7 d interval.
    now = _local(BLOOM + 12 * D, 6)
    spray = Spray(ts=now - 8 * D, products=(SULPHUR,))

    advice = evaluate(_flat(now, 25.0), [spray], CONFIG, now)

    assert advice.reason("powdery_gubler_thomas").inputs["interval_days"] == 7
    assert advice.decision == "spray"
    assert [p.name for p in advice.products_recommended] == ["sulphur"]
    assert "expired" in advice.flags


def test_sulphur_due_while_downy_is_covered():
    now = _local(BLOOM + 5 * D, 6)
    rain_at = now - 30 * H
    rows = series(_local(BLOOM - 20 * D, 0), now + 80 * H, t=25.0, overrides=rain(rain_at, 1.0, 10))
    copper = Spray(ts=now - 12 * H, products=(COPPER,))

    advice = evaluate(rows, [copper], CONFIG, now)

    assert advice.reason("downy_3_10").verdict == "covered"
    assert advice.decision == "spray"
    assert [p.name for p in advice.products_recommended] == ["sulphur"]
    # On a spray/wait advice, protected_until is the downy cover (decision.py docstring).
    assert advice.protected_until == copper.ts + 7 * D  # rapid growth
    assert advice.reason("powdery_pass").inputs["downy_protected_until"] == copper.ts + 7 * D


# --- No future leak in the powdery period (05's property, extended to incubation and GT) -----


def _random_obs(rng: random.Random, ts, wet: bool) -> HourlyObs:
    return HourlyObs(
        ts=ts,
        rr1_mm=None if rng.random() < 0.01 else (round(rng.uniform(0, 2), 1) if wet else 0.0),
        t_c=round(rng.uniform(14, 37), 1),
        rh_pct=round(rng.uniform(40, 100)),
        ff_kmh=round(rng.uniform(0, 30), 1),
    )


def _period_scenario(seed: int):
    rng = random.Random(seed)
    span = int((_local(CONFIG.powdery_end.value, 0) - _local(BLOOM, 0)) / H)
    now = _local(BLOOM, 0) + rng.randrange(0, span) * H
    rows, ts, wet = [], now - 30 * D, False
    while ts <= now + 3 * H:  # enough for "open now" (2 h run + 2 h lookahead)
        if ts.hour == 0:
            wet = rng.random() < 0.25
        rows.append(_random_obs(rng, ts, wet and rng.random() < 0.6))
        ts += H
    sprays = [
        Spray(
            ts=now - rng.randrange(0, 30 * 24) * H,
            products=tuple(rng.sample([COPPER, SULPHUR], rng.randint(1, 2))),
        )
        for _ in range(rng.randint(0, 3))
    ]
    future = [_random_obs(rng, ts + k * H, rng.random() < 0.3) for k in range(rng.randint(1, 80))]
    return now, rows, sprays, future


def _past_only(advice):
    return (
        [r for r in advice.reasons if r.rule != "spray_window"],
        advice.protected_until,
        advice.mm_since_spray,
        advice.flags - {"replay_forecast"},
    )


@pytest.mark.parametrize("seed", range(40))
def test_hours_after_now_do_not_move_incubation_gt_or_covers(seed):
    now, rows, sprays, future = _period_scenario(seed)

    base = evaluate(rows, sprays, CONFIG, now)
    extended = evaluate(rows + future, sprays, CONFIG, now)
    truncated = evaluate([o for o in rows if o.ts <= now], sprays, CONFIG, now)

    assert base.reason("powdery_gubler_thomas").inputs["in_period"] is True
    assert extended.decision == base.decision
    assert _past_only(extended) == _past_only(base)
    assert _past_only(truncated) == _past_only(base)


def test_outside_the_period_there_is_no_powdery_need():
    now = _local(BLOOM - 10 * D, 6)
    rows = series(_local(BLOOM - 20 * D, 0), now + 6 * H)

    advice = evaluate(rows, [], CONFIG, now)

    assert advice.decision == "none"
    assert advice.reason("powdery_gubler_thomas").verdict == "outside powdery period"
    assert advice.products_recommended == []
