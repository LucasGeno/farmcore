"""Cross-rule invariants through `evaluate`: no future leak, config echo, explained reasons."""

import random
from datetime import date, timedelta

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs, ProductRef, Spray, evaluate
from farmcore.apps.advisor.engine.config import Threshold
from tests.apps.advisor.engine.synth import merge, rain, series, utc

H = timedelta(hours=1)
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
COPPER = ProductRef(name="copper", product_type="contact", targets=("downy",))
SULPHUR = ProductRef(name="sulphur", product_type="contact", targets=("powdery",))
PENETRANT = ProductRef(name="penetrant-x", product_type="penetrant", targets=("downy",))


def _random_obs(rng: random.Random, ts, wet_day: bool = False) -> HourlyObs:
    wet = wet_day and rng.random() < 0.6
    return HourlyObs(
        ts=ts,
        rr1_mm=None if rng.random() < 0.01 else (round(rng.uniform(0, 2), 1) if wet else 0.0),
        t_c=round(rng.uniform(5, 34), 1),
        rh_pct=round(rng.uniform(40, 100)),
        ff_kmh=round(rng.uniform(0, 30), 1),
    )


def _scenario(seed: int):
    rng = random.Random(seed)
    now = utc(2024, 4, 9, 0) + rng.randrange(0, 24 * 40) * H
    start = utc(2024, 3, 25)
    rows = []
    ts = start
    wet_day = False
    while ts <= now + 3 * H:  # enough for the "open now" run (2 h + 2 h lookahead)
        if ts.hour == 0:
            wet_day = rng.random() < 0.25  # rain comes in spells, so 3-10 re-arms
        rows.append(_random_obs(rng, ts, wet_day))
        ts += H
    sprays = [
        Spray(
            ts=start + rng.randrange(0, int((now - start) / H) + 48) * H,
            products=tuple(rng.sample([COPPER, SULPHUR, PENETRANT], rng.randint(1, 2))),
        )
        for _ in range(rng.randint(0, 4))
    ]
    future = [_random_obs(rng, ts + k * H) for k in range(rng.randint(1, 100))]
    return now, rows, sprays, future


def _without_window(advice):
    return (
        [r for r in advice.reasons if r.rule != "spray_window"],
        advice.protected_until,
        advice.mm_since_spray,
        advice.flags - {"replay_forecast"},
    )


@pytest.mark.parametrize("seed", range(60))
def test_hours_after_now_change_only_next_window_and_replay_flag(seed):
    now, rows, sprays, future = _scenario(seed)

    base = evaluate(rows, sprays, CONFIG, now)
    extended = evaluate(rows + future, sprays, CONFIG, now)
    truncated = evaluate([o for o in rows if o.ts <= now], sprays, CONFIG, now)

    # Beyond the hours that decide "open now", future hours move only the next window.
    assert extended.decision == base.decision
    assert _without_window(extended) == _without_window(base)
    # With no future at all, only the window can differ (so spray may read wait).
    assert _without_window(truncated) == _without_window(base)
    assert {truncated.decision, base.decision} <= {"spray", "wait"} or (
        truncated.decision == base.decision
    )
    assert "replay_forecast" not in truncated.flags


def _changed(value):
    if isinstance(value, date):
        return value + timedelta(days=1)
    return value + 1


def test_every_rule_result_echoes_the_config_value():
    changed = CONFIG.model_copy(
        update={
            name: Threshold(value=_changed(getattr(CONFIG, name).value), source=f"test {name}")
            for name in AdvisorConfig.threshold_names()
        }
    )
    now = utc(2024, 4, 9, 12)
    s = series(
        utc(2024, 3, 25), now + 80 * H, t=15.0, overrides=merge(rain(utc(2024, 4, 8), 1.0, 20))
    )
    sprays = [
        Spray(ts=utc(2024, 3, 20), products=(COPPER,)),
        Spray(ts=utc(2024, 4, 1), products=(SULPHUR,)),
    ]

    advice = evaluate(s, sprays, changed, now)

    rules = {r.rule for r in advice.reasons}
    assert rules >= {
        "season_gate",
        "downy_3_10",
        "protection_downy",
        "protection_powdery",
        "spray_window",
    }
    echoed: set[str] = set()
    for reason in advice.reasons:
        for name, value in reason.threshold.items():
            assert value == getattr(changed, name).value, (reason.rule, name)
            assert value != getattr(CONFIG, name).value, (reason.rule, name)
            assert f"test {name}" in reason.source
            echoed.add(name)
    assert echoed == set(AdvisorConfig.threshold_names())


def test_every_reason_carries_inputs_threshold_verdict_and_source():
    now = utc(2024, 4, 9, 12)
    s = series(utc(2024, 3, 25), now + 80 * H, overrides=rain(utc(2024, 4, 8), 1.0, 20))

    for sprays in ([], [Spray(ts=utc(2024, 4, 7), products=(COPPER, SULPHUR))]):
        for reason in evaluate(s, sprays, CONFIG, now).reasons:
            assert reason.inputs, reason.rule
            assert reason.threshold, reason.rule
            assert reason.verdict, reason.rule
            assert reason.source, reason.rule


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        evaluate([], [], CONFIG, utc(2024, 4, 9).replace(tzinfo=None))


@pytest.mark.parametrize(
    "now,status",
    [(utc(2024, 4, 20, 12), "unavailable"), (utc(2024, 3, 20), "evaluable")],
)
def test_empty_observations_are_unavailable_in_season_and_a_gate_before_it(now, status):
    advice = evaluate([], [], CONFIG, now)
    assert advice.assessment_status == status
    if status == "evaluable":
        assert advice.reason("season_gate").verdict == "season not started"
        return
    assert advice.reason("downy_3_10").verdict == "not evaluable"
    assert advice.reason("input_availability").inputs["missing"] == ["hourly_rain_temperature"]
    assert advice.protected_until is None
    assert advice.passes == advice.treatments == 0


def _downy_rows(missing):
    now = utc(2024, 4, 20, 12)
    rows = series(utc(2024, 3, 25), now, t=15)
    if missing == "stopped":
        rows = rows[:-6]
    elif missing == "one hour":
        rows = [o for o in rows if o.ts != now - 30 * H]
    elif missing == "odd hours duplicated":
        rows = [copy for o in rows if o.ts.hour % 2 == 0 for copy in (o, o, o)]
    elif missing != "nothing":
        rows = [
            HourlyObs(
                o.ts,
                None if missing == "rain" else o.rr1_mm,
                None if missing == "temperature" else o.t_c,
                o.rh_pct,
                o.ff_kmh,
            )
            for o in rows
        ]
    # Rows after now never heal a past gap.
    return now, rows + series(now + H, now + 80 * H, t=15), [], "downy_3_10"


def _powdery_rows(gap):
    now = utc(2024, 6, 5, 12)
    day = date(2024, 5, 29)
    rows = series(utc(2024, 3, 25), now + 80 * H, t=15, overrides=rain(utc(2024, 6, 4), 1.0, 10))
    if gap == "null day":
        rows = [
            HourlyObs(o.ts, o.rr1_mm, None if o.ts.date() == day else o.t_c, o.rh_pct, o.ff_kmh)
            for o in rows
        ]
    else:
        rows = [
            copy
            for o in rows
            if o.ts.date() != day or o.ts.hour % 2 == 0
            for copy in ((o, o, o) if o.ts.date() == day else (o,))
        ]
    return now, rows, [Spray(utc(2024, 6, 3), (COPPER,))], "powdery_gubler_thomas"


def _cover_rows(gap):
    """A contact cover is only protected while every hour since the spray is known rain."""
    now = utc(2024, 4, 17, 12)
    rows = series(utc(2024, 3, 25), now + 80 * H, t=15, overrides=rain(utc(2024, 4, 16), 1.0, 10))
    hole = utc(2024, 4, 12)
    sprayed = utc(2024, 4, 11, 12)
    if gap == "null":
        rows = [
            HourlyObs(o.ts, None if o.ts == hole else o.rr1_mm, o.t_c, o.rh_pct, o.ff_kmh)
            for o in rows
        ]
    elif gap == "conflicting duplicate":
        rows.append(HourlyObs(hole, 40, 15, 70, 5))
    else:
        rows = [o for o in rows if o.ts != hole]
        if gap == "removed + duplicate":
            rows.append(next(o for o in rows if o.ts == hole + H))
        elif gap == "removed + off-grid":
            rows.append(HourlyObs(hole + H / 2, 0, 15, 70, 5))
        else:
            sprayed += H / 2
    return now, rows, [Spray(sprayed, (COPPER,))], "protection_downy"


INCOMPLETE = [
    *(("downy", m) for m in ("rain", "temperature", "stopped", "one hour", "odd hours duplicated")),
    *(("powdery", g) for g in ("null day", "duplicated half day")),
    *(
        ("cover", g)
        for g in (
            "null",
            "removed + duplicate",
            "removed + off-grid",
            "removed + partial-hour spray",
            "conflicting duplicate",
        )
    ),
    ("downy", "nothing"),  # control: complete dry observations still establish no risk
]


@pytest.mark.parametrize("model,gap", INCOMPLETE)
def test_incomplete_inputs_never_establish_no_risk_low_pressure_or_protection(model, gap):
    build = {"downy": _downy_rows, "powdery": _powdery_rows, "cover": _cover_rows}[model]
    now, rows, sprays, rule = build(gap)
    advice = evaluate(rows, sprays, CONFIG, now)
    if gap == "nothing":
        assert advice.assessment_status == "evaluable"
        assert advice.reason(rule).verdict == "no risk"
        return
    assert advice.assessment_status == "limited"
    assert advice.decision != "protected"
    assert advice.protected_until is None
    if model == "cover":
        assert advice.reason("downy_3_10").verdict == "risk"
        assert advice.reason(rule).inputs["rain_data_gap"] is True
    else:
        assert advice.reason(rule).verdict == "not evaluable"


def test_missing_rain_does_not_erase_observed_positive_infection():
    now = utc(2024, 4, 9, 12)
    rows = series(utc(2024, 3, 25), now + 80 * H, t=15, overrides=rain(utc(2024, 4, 8), 1.0, 20))
    rows = [
        HourlyObs(
            o.ts, None if o.ts == utc(2024, 4, 9, 11) else o.rr1_mm, o.t_c, o.rh_pct, o.ff_kmh
        )
        for o in rows
    ]
    advice = evaluate(rows, [], CONFIG, now)
    assert advice.assessment_status == "limited"
    assert advice.reason("downy_3_10").verdict == "risk"
    assert advice.decision == "spray"


def test_old_incubation_gap_limits_that_reason_without_invalidating_current_lookback():
    now = utc(2024, 4, 20, 12)
    rows = series(utc(2024, 3, 25), now, t=15, rh=95, overrides=rain(utc(2024, 4, 8), 1.0, 10))
    rows = [
        HourlyObs(o.ts, o.rr1_mm, None if o.ts == utc(2024, 4, 10) else o.t_c, o.rh_pct, o.ff_kmh)
        for o in rows
    ]
    advice = evaluate(rows, [], CONFIG, now)
    assert advice.assessment_status == "evaluable"
    assert advice.reason("downy_3_10").verdict == "no risk"
    incubation = advice.reason("downy_incubation")
    assert incubation.verdict == "not evaluable"
    assert incubation.inputs["coverage"] == "limited"
    assert incubation.inputs["event_history_coverage"] == "limited"
    assert incubation.inputs["pct_basis"] == "observed-hours-only"
    assert incubation.inputs["expected_symptoms"] is None
    assert incubation.inputs["symptoms_at"] is None
    assert incubation.inputs["projected"] is False


@pytest.mark.parametrize(
    "product,sprayed,decision",
    [(COPPER, utc(2024, 4, 1), "spray"), (PENETRANT, utc(2024, 4, 11, 12), "protected")],
)
def test_old_rain_gap_does_not_invalidate_expired_or_rainfast_cover(product, sprayed, decision):
    now = utc(2024, 4, 17, 12)
    rows = series(utc(2024, 3, 25), now + 80 * H, t=15, overrides=rain(utc(2024, 4, 16), 1.0, 10))
    rows = [
        HourlyObs(o.ts, None if o.ts == utc(2024, 4, 12) else o.rr1_mm, o.t_c, o.rh_pct, o.ff_kmh)
        for o in rows
    ]
    advice = evaluate(rows, [Spray(sprayed, (product,))], CONFIG, now)
    assert advice.assessment_status == "evaluable"
    assert advice.decision == decision
    assert advice.reason("downy_3_10").verdict == "risk"


@pytest.mark.parametrize(
    "case", ["dry duplicates", "rain duplicates", "partial-hour spray in CEST"]
)
def test_complete_contact_cover_rain_stays_known_and_counted_once(case):
    from zoneinfo import ZoneInfo

    now = utc(2024, 4, 17, 12)
    rows = series(utc(2024, 3, 25), now + 80 * H, t=15, overrides=rain(utc(2024, 4, 16), 1.0, 10))
    sprayed = utc(2024, 4, 11, 12)
    if case == "partial-hour spray in CEST":
        sprayed = (sprayed + H / 2).astimezone(ZoneInfo("Europe/Paris"))
    else:
        copied = utc(2024, 4, 16 if case == "rain duplicates" else 12)
        rows.extend([next(o for o in rows if o.ts == copied)] * 40)
    advice = evaluate(rows, [Spray(sprayed, (COPPER,))], CONFIG, now)
    assert advice.assessment_status == "evaluable"
    assert advice.decision == "protected"
    assert advice.reason("protection_downy").inputs["mm_since_spray"] == 10.0
    assert advice.reason("protection_downy").inputs["rain_data_gap"] is False
