"""Priority (Due · High · Urgent) from a stored advice row only: a ranking, never a probability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.advisor import priority as prio

T = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
EVENT = T - timedelta(hours=30)


def _reasons(*, active=True, cover="no cover", pct=1.2, symptoms=T + timedelta(days=100)):
    return [
        {
            "rule": "downy_3_10",
            "verdict": "risk" if active else "covered",
            "inputs": {
                "event_at": EVENT.isoformat(),
                "active_events": [EVENT.isoformat()] if active else [],
            },
        },
        {"rule": "protection_downy", "verdict": cover, "inputs": {}},
        {
            "rule": "downy_incubation",
            "verdict": "incubating",
            "inputs": {"pct": pct, "expected_symptoms": symptoms.isoformat()},
            "threshold": {"incub_alert_pct": 50.0, "incub_symptoms_pct": 100.0},
        },
    ]


def _p(decision="spray", reasons=None, window=T, evaluated=T):
    return prio.priority(
        decision, _reasons() if reasons is None else reasons, next_window_start=window,
        evaluated_at=evaluated,
    )  # fmt: skip


@pytest.mark.parametrize("decision", ["protected", "none"])
def test_no_action_needed_has_no_priority(decision):
    assert _p(decision) is None


def test_uncovered_a_day_or_more_under_active_risk_is_two():
    p = _p()  # 30 h since the uncovered event, incubation early, a window opens now

    assert (p.level, p.label) == (2, "High")
    assert p.assumption is True
    assert "30 h" in p.why and "%" not in p.why


def test_incubation_past_the_alert_stage_is_three():
    p = _p(reasons=_reasons(pct=55.0))

    assert (p.level, p.label) == (3, "Urgent")
    assert "alert stage" in p.why


def test_no_spray_window_before_incubation_ends_is_three():
    soon = T + timedelta(hours=20)
    late_window = T + timedelta(hours=30)

    assert _p(reasons=_reasons(symptoms=soon), window=late_window).level == 3
    assert _p(reasons=_reasons(symptoms=soon), window=None).level == 3
    assert _p(reasons=_reasons(symptoms=soon), window=T + timedelta(hours=2)).level == 2


def test_recent_uncovered_risk_is_one():
    p = _p(evaluated=EVENT + timedelta(hours=10))

    assert (p.level, p.label) == (1, "Due")
    assert "10 h" in p.why


def test_a_pass_due_without_uncovered_downy_risk_is_one():
    for reasons in (_reasons(active=False), _reasons(cover="protected"), []):
        p = _p("wait", reasons=reasons)
        assert (p.level, p.label) == (1, "Due")
        assert p.why


def test_as_dict_is_the_json_contract():
    assert prio.as_dict(_p()) == {
        "priority": 2,
        "priority_label": "High",
        "priority_why": _p().why,
        "priority_assumption": True,
    }
    assert prio.as_dict(None) == {
        "priority": None,
        "priority_label": None,
        "priority_why": None,
        "priority_assumption": False,
    }


@pytest.mark.parametrize(
    "pct,window,level",
    [(55.0, None, 3), (1.2, T, 2)],  # observed lower bound past the alert; still before it
)
def test_limited_incubation_states_unknown_timing_and_invents_no_window(pct, window, level):
    reasons = _reasons(pct=pct, symptoms=T)
    reasons[2]["inputs"] |= {"coverage": "limited", "expected_symptoms": None}

    p = _p("wait", reasons=reasons, window=window)

    assert p.level == level
    assert "Incubation timing is not evaluable" in p.why
    assert "before the alert stage" not in p.why
    assert "opens before it ends" not in p.why
