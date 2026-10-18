"""`kpi.service.kpis` on hand-built season results (pure, no DB)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from farmcore.apps.advisor.engine.simulate import (
    CalendarPass,
    CalendarResult,
    SeasonResult,
    SimPass,
)
from farmcore.apps.kpi.config import Assumptions
from farmcore.apps.kpi.service import kpis

DAY0 = date(2024, 4, 15)


def _model(*passes: tuple[str, ...]) -> SeasonResult:
    sims = tuple(
        SimPass(
            ts=datetime(2024, 4, 15, 6, tzinfo=UTC) + timedelta(days=7 * i),
            day=DAY0 + timedelta(days=7 * i),
            advised_at=datetime(2024, 4, 15, 4, tzinfo=UTC) + timedelta(days=7 * i),
            decision="spray",
            products=products,
            treatments=len(products),
            reasons=(),
        )
        for i, products in enumerate(passes)
    )
    return SeasonResult(
        form="hourly",
        start=DAY0,
        end=date(2024, 6, 30),
        passes=len(sims),
        treatments=sum(p.treatments for p in sims),
        pressure_days=0,
        window_days=0,
        days_evaluated=77,
        dates=sims,
        labels=(),
        threshold={},
        source="",
    )


def _calendar(*passes: tuple[str, ...]) -> CalendarResult:
    dates = tuple(CalendarPass(DAY0 + timedelta(days=7 * i), p) for i, p in enumerate(passes))
    return CalendarResult(
        year=2024,
        start=DAY0,
        end=date(2024, 6, 30),
        passes=len(dates),
        treatments=sum(len(p.products) for p in dates),
        dates=dates,
        threshold={},
        source="",
    )


CU, S, MIX = ("copper",), ("sulphur",), ("copper", "sulphur")
# Model 11 passes / 12 treatments: 6 copper + 6 sulphur (one tank-mixed pass).
MODEL = _model(MIX, *[CU] * 5, *[S] * 5)
# Calendar 13 passes / 20 treatments: 13 copper + 7 sulphur (7 tank-mixed passes).
CALENDAR = _calendar(*[MIX] * 7, *[CU] * 6)


# The product names the season's config sprays (the route passes them from `SeasonYear`).
PRODUCTS = {"copper": "copper", "sulphur": "sulphur"}


def _by_key(result):
    return result.by_key()


def test_fixture_counts_match_the_worked_example():
    assert (MODEL.passes, MODEL.treatments) == (11, 12)
    assert (CALENDAR.passes, CALENDAR.treatments) == (13, 20)


def test_treatments_avoided_is_agreste_minus_model_treatments():
    k = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))["treatments_avoided"]
    assert k.value == pytest.approx(8.1)
    assert "20.1" in k.formula and "12" in k.formula


# A worked example (defaults, 4 voice ops, 25.59 ha): independent literals.
WORKED = {
    "passes_avoided": 2,
    "eur_ha": 157.2,  # 2 × 30 + 8.1 × 12
    "diesel_l_ha": 17.4,  # 2 × 8.7 (Chambres d'agriculture + Fédération Cuma Bourgogne)
    "co2_kg_ha": 55.158,  # 17.4 × 3.17 (GNR, Arrêté du 26 avril 2017)
    "cu_kg_ha": 2.31,  # (13 − 6) × 0.33
    "s_kg_ha": 3.0,  # (7 − 6) × 3
    "minutes_saved": 10.0,  # 4 × (3 − 0.5)
    "eur_farm": 4022.748,  # 157.2 × 25.59
    "diesel_l_farm": 445.266,  # 17.4 × 25.59
    "co2_kg_farm": 1411.49322,  # 55.158 × 25.59
}


@pytest.mark.parametrize(("key", "expected"), sorted(WORKED.items()))
def test_worked_example_value_and_formula(key, expected):
    k = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))[key]
    assert k.value == pytest.approx(expected)
    assert k.formula and k.label and k.unit


def test_co2_kpis_are_co2e_like_the_gnr_factor():
    k = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))
    assert (k["co2_kg_ha"].unit, k["co2_kg_farm"].unit) == ("kg CO₂e/ha", "kg CO₂e")
    assert "CO₂e" in k["co2_kg_ha"].label and "CO₂e" in k["co2_kg_farm"].label


def test_eur_ha_formula_shows_its_inputs():
    k = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))["eur_ha"]
    assert k.formula == (
        "2 passes avoided × 30 €/ha/pass (machine + labour per pass) + 8.1 treatments avoided "
        "× 12 €/ha/treatment (product cost per treatment) = 157.2 €/ha"
    )


def test_a_model_worse_than_the_calendar_gives_negative_values_unclamped():
    worse = _model(*[MIX] * 14)  # 14 passes, 28 treatments: 14 copper + 14 sulphur
    k = _by_key(kpis(worse, CALENDAR, 0, 10, Assumptions(), **PRODUCTS))
    assert k["passes_avoided"].value == -1
    assert k["treatments_avoided"].value == pytest.approx(-7.9)
    assert k["diesel_l_ha"].value == pytest.approx(-8.7)
    assert k["cu_kg_ha"].value == pytest.approx(-0.33)
    assert k["s_kg_ha"].value == pytest.approx(-21.0)
    assert k["eur_ha"].value == pytest.approx(-1 * 30 + -7.9 * 12)


def test_edited_assumption_changes_eur_ha_by_passes_avoided_times_the_delta():
    base = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))["eur_ha"].value
    edited = Assumptions().with_values(pass_cost_eur_ha=40)
    assert edited.pass_cost_eur_ha.value == 40
    assert Assumptions().pass_cost_eur_ha.value == 30  # defaults never change
    new = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, edited, **PRODUCTS))["eur_ha"].value
    assert new - base == pytest.approx(2 * 10)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), 1e9])
def test_assumption_outside_its_bounds_is_refused(value):
    with pytest.raises(ValueError, match="form_minutes"):
        Assumptions().with_values(form_minutes=value)


def test_every_assumption_has_value_unit_label_and_source():
    a = Assumptions()
    for name in Assumptions.names():
        field = getattr(a, name)
        assert field.unit and field.label and field.source
        assert field.min <= field.value <= field.max


def test_cu_and_s_count_the_product_names_they_are_given():
    swap = {"copper": "bouillie", "sulphur": "thiovit"}

    def renamed(*passes):
        return [tuple(swap[x] for x in p) for p in passes]

    model = _model(*renamed(MIX, *[CU] * 5, *[S] * 5))
    calendar = _calendar(*renamed(*[MIX] * 7, *[CU] * 6))

    k = kpis(model, calendar, 4, 25.59, Assumptions(), copper="bouillie", sulphur="thiovit")

    assert (k.model_copper, k.model_sulphur, k.calendar_copper, k.calendar_sulphur) == (6, 6, 13, 7)
    assert k.by_key()["cu_kg_ha"].value == pytest.approx(2.31)
    # Another name (e.g. the engine's class default) counts nothing on this season.
    assert kpis(model, calendar, 4, 25.59, Assumptions(), **PRODUCTS).model_copper == 0


# The page prints formulas as they are; they must read as plain words, rounded to the
# precision the tile shows, and say whether they rest on an assumed value.
CONFIG_KEYS = (*Assumptions.names(), "calendar.downy_interval_days")


def _worked():
    return kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS).kpis


@pytest.mark.parametrize("k", _worked(), ids=lambda k: k.key)
def test_formulas_name_inputs_in_words_not_config_keys(k):
    assert not [key for key in CONFIG_KEYS if key in k.formula]


def test_formulas_use_the_assumption_labels():
    a, k = Assumptions(), _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))
    assert a.pass_cost_eur_ha.label in k["eur_ha"].formula
    assert a.product_cost_eur_ha.label in k["eur_ha"].formula
    assert a.diesel_l_ha_pass.label in k["diesel_l_ha"].formula
    assert a.co2_kg_per_l.label in k["co2_kg_ha"].formula
    assert a.cu_kg_ha_treatment.label in k["cu_kg_ha"].formula


@pytest.mark.parametrize("k", _worked(), ids=lambda k: k.key)
def test_formula_numbers_are_rounded_to_the_displayed_precision(k):
    decimals = [len(m) for m in re.findall(r"\d\.(\d+)", k.formula)]
    assert max(decimals, default=0) <= 2, k.formula
    shown = f"{k.value:,.{k.digits}f}"
    shown = shown.rstrip("0").rstrip(".") if k.digits else shown
    assert k.formula.endswith(f"= {shown} {k.unit}"), k.formula


def test_farm_totals_round_to_whole_units():
    k = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))
    assert k["eur_farm"].formula.endswith("= 4,023 €")
    assert k["co2_kg_farm"].formula.endswith("= 1,411 kg CO₂e")
    assert "4,022.748" not in k["eur_farm"].formula


def test_only_figures_resting_on_an_assumed_value_are_flagged_assumed():
    k = _by_key(kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), **PRODUCTS))
    # Agreste is sourced and the model is measured: the only figure with no assumed input.
    assert [x.key for x in k.values() if not x.assumed] == ["treatments_avoided"]
    sourced_calendar = _by_key(
        kpis(MODEL, CALENDAR, 4, 25.59, Assumptions(), calendar_assumed=False, **PRODUCTS)
    )
    # Diesel and CO₂e factors are sourced: with a sourced calendar they are not assumed …
    assert not sourced_calendar["diesel_l_ha"].assumed
    assert not sourced_calendar["co2_kg_farm"].assumed
    # … while €, copper dose and typing minutes still are.
    assert sourced_calendar["eur_ha"].assumed and sourced_calendar["cu_kg_ha"].assumed
    assert sourced_calendar["minutes_saved"].assumed


def test_sourced_and_assumed_assumptions_are_told_apart():
    a = Assumptions()
    assert not a.diesel_l_ha_pass.assumed and not a.co2_kg_per_l.assumed
    for name in ("pass_cost_eur_ha", "product_cost_eur_ha", "form_minutes", "voice_minutes"):
        assert getattr(a, name).assumed
        assert getattr(a, name).source == "assumed: awaiting agronomist"
