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
