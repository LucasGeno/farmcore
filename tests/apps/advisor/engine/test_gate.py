from datetime import date

from farmcore.apps.advisor.engine import AdvisorConfig, evaluate
from tests.apps.advisor.engine.synth import rain, series, utc


def test_before_gate_is_none_with_season_not_started_and_assumed_gate_date():
    config = AdvisorConfig.for_region_year("FRI12", 2024)
    # A storm before the gate must not matter: the season has not started.
    s = series(utc(2024, 4, 1), utc(2024, 4, 4, 12), overrides=rain(utc(2024, 4, 2), 2.0, 10))

    advice = evaluate(s, [], config, utc(2024, 4, 4, 12))

    assert advice.decision == "none"
    gate = next(r for r in advice.reasons if r.rule == "season_gate")
    assert gate.verdict == "season not started"
    assert gate.threshold == {"season_gate": date(2024, 4, 5)}
    assert "assumed" in gate.note
    assert gate.source


def test_gate_dates_per_region_year():
    assert AdvisorConfig.for_region_year("FRI12", 2024).season_gate.value == date(2024, 4, 5)
    assert AdvisorConfig.for_region_year("FRI12", 2025).season_gate.value == date(2025, 4, 15)
    # ITF63 (Catanzaro) has no season of its own: generic default (ES418 has one since 21).
    other = AdvisorConfig.for_region_year("ITF63", 2026).season_gate
    assert other.value == date(2026, 4, 15)
    assert other.assumed
