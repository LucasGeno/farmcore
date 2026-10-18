"""`kpi.service.copper_lead`: the KPI page's lead figure on hand-built seasons (pure, no DB).

Copper treatments avoided vs a calendar, the change in %, and kg Cu/ha at the editable dose per
treatment. Expected values are worked by hand: model 7 copper vs a 10-copper calendar → 3
avoided, −30 %, 3 × 0.33 = 0.99 kg Cu/ha; vs 13 → 6, −46.15 %, 1.98.
"""

from __future__ import annotations

import pytest

from farmcore.apps.kpi.config import Assumptions
from farmcore.apps.kpi.service import copper_lead
from tests.apps.kpi.test_service import CU, MIX, S, _calendar, _model

# 7 copper treatments (one tank-mixed with sulphur) + 2 sulphur-only passes.
MODEL = _model(MIX, *[CU] * 6, S, S)
PLAIN_10 = _calendar(*[CU] * 10)
RAPID_12B = _calendar(*[MIX] * 7, *[CU] * 6)  # 13 copper


def test_lead_vs_a_plain_ten_copper_calendar_is_3_avoided_minus_30_pct_0_99_kg():
    lead = copper_lead(
        MODEL, PLAIN_10, Assumptions(), copper="copper", key="plain", label="plain", at_least=True
    )

    assert (lead.model_copper, lead.calendar_copper) == (7, 10)
    assert lead.avoided == 3
    assert lead.pct == pytest.approx(-30.0)
    assert lead.kg_cu_ha == pytest.approx(0.99)
    assert lead.at_least is True
    assert "0.33" in lead.formula and "kg Cu" in lead.formula


def test_lead_vs_the_rapid_growth_calendar_is_6_avoided_minus_46_pct_1_98_kg():
    lead = copper_lead(MODEL, RAPID_12B, Assumptions(), copper="copper", key="12b", label="12b")

    assert (lead.avoided, round(lead.pct), lead.kg_cu_ha) == (6, -46, pytest.approx(1.98))
    assert lead.at_least is False


def test_lead_follows_the_edited_copper_dose():
    a = Assumptions().with_values(cu_kg_ha_treatment=0.5)

    lead = copper_lead(MODEL, PLAIN_10, a, copper="copper", key="plain", label="plain")

    assert lead.kg_cu_ha == pytest.approx(1.5)


def test_a_calendar_without_copper_has_no_percentage():
    lead = copper_lead(_model(S), _calendar(S), Assumptions(), copper="copper", key="k", label="l")

    assert (lead.avoided, lead.pct, lead.kg_cu_ha) == (0, None, 0)


@pytest.mark.parametrize(("days", "word"), [(10.0, "10-day"), (12.0, "12-day"), (8.5, "8.5-day")])
def test_plain_calendar_label_follows_the_downy_interval_and_has_no_markdown(days, word):
    from farmcore.apps.kpi.config import plain_calendar_label

    label = plain_calendar_label(days)

    assert word in label
    assert "`" not in label
