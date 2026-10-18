"""Register checks: label and copper rules per spray, pure (no database).

Synthetic sprays; the label values mirror the seed shortlist (E-Phy, vine) but are passed in.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from farmcore.apps.operations import checks
from farmcore.apps.operations.checks import Application, Label

COPPER = Label(max_dose_kg_ha=Decimal("3.75"), max_applications=5, cu_fraction=Decimal("0.2"))
SULPHUR = Label(max_dose_kg_ha=Decimal("12.5"), max_applications=8, phi_days=3)
PAJO = Label(max_dose_kg_ha=Decimal("0.45"), max_applications=2, phi_days=28, min_interval_days=7)
PRODUCTS = {
    1: ("BOUILLIE BORDELAISE RSR DISPERSS NC", "copper", COPPER),
    2: ("MICROTHIOL SPECIAL DISPERSS", "sulphur", SULPHUR),
    3: ("PAJO", "cymoxanil", PAJO),
}


def app(log_id: int, day: str, parcel: str = "P1", product: int = 1, dose: str | None = "2"):
    d = date.fromisoformat(day)
    name, substance, label = PRODUCTS[product]
    return Application(
        log_id=log_id,
        ts=datetime(d.year, d.month, d.day, 6, tzinfo=UTC),
        day=d,
        parcel=parcel,
        product_id=product,
        product_name=name,
        substance=substance,
        dose_kg_ha=None if dose is None else Decimal(dose),
        label=label,
    )


def by_key(log_id: int, apps: list[Application]) -> dict[str, checks.Check]:
    return {c.key: c for c in checks.spray_checks(log_id, apps)}


# --- copper (Reg. (EU) 2018/1981) -------------------------------------------------------------


def test_copper_is_kg_cu_per_ha_from_the_product_fraction():
    c = by_key(1, [app(1, "2024-05-09")])["copper"]
    assert (c.status, c.label) == ("ok", "Copper 0.4 of 4 kg Cu/ha this year")
    assert "2018/1981" in c.detail


def test_copper_at_exactly_4_kg_is_ok_and_above_is_a_warning():
    # 10 kg/ha of a 20 % product = 2 kg Cu/ha per spray.
    apps = [app(i, f"2024-05-0{i}", dose="10") for i in (1, 2, 3)]
    at_four = by_key(2, apps)["copper"]
    above = by_key(3, apps)["copper"]
    assert (at_four.status, at_four.label) == ("ok", "Copper 4 of 4 kg Cu/ha this year")
    assert (above.status, above.label) == ("warn", "Copper 6 of 4 kg Cu/ha this year")
    assert "Above the 4 kg Cu/ha yearly average" in above.detail
    assert "28 kg Cu/ha over 7 years" in above.detail


def test_copper_counts_this_parcel_this_year_up_to_this_spray_only():
    apps = [
        app(1, "2023-06-01", dose="10"),  # last year
        app(2, "2024-05-01", parcel="P2", dose="10"),  # another parcel
        app(3, "2024-05-09"),  # this spray
        app(4, "2024-06-01", dose="10"),  # later
    ]
    assert by_key(3, apps)["copper"].label == "Copper 0.4 of 4 kg Cu/ha this year"


def test_copper_retains_known_sum_as_lower_bound_when_earlier_dose_is_unknown():
    other_copper = Label(cu_fraction=Decimal("0.5"))
    apps = [
        app(1, "2024-05-01", dose=None),
        replace(app(2, "2024-05-02"), product_id=9, label=other_copper),
        app(3, "2024-05-09"),
    ]
    c = by_key(3, apps)["copper"]
    assert c.status == "warn"
    assert c.label == "Copper at least 1.4 kg Cu/ha this year (incomplete)"
    assert "lower bound" in c.detail


def test_unknown_current_copper_dose_never_certifies_zero():
    c = by_key(1, [app(1, "2024-05-09", dose=None)])["copper"]
    assert c.status == "warn"
    assert "incomplete" in c.label
    assert "unknown" in c.detail.lower()


def test_a_spray_without_copper_has_no_copper_check():
    apps = [app(1, "2024-05-01"), app(2, "2024-05-09", product=2)]
    assert "copper" not in by_key(2, apps)


# --- applications this season -----------------------------------------------------------------


def test_applications_count_this_product_on_the_parcel_this_season():
    apps = [
        app(1, "2023-05-01"),
        app(2, "2024-04-10"),
        app(3, "2024-04-20", product=2),
        app(4, "2024-05-09"),
        app(5, "2024-05-20", parcel="P2"),
    ]
    c = by_key(4, apps)["applications:1"]
    assert (c.status, c.label) == ("ok", "2 of 5 copper applications this season")
    assert "BOUILLIE BORDELAISE RSR DISPERSS NC" in c.detail


def test_applications_over_the_label_max_warn():
    apps = [app(i, f"2024-05-{10 + 8 * i}", product=3, dose="0.4") for i in range(3)]
    assert by_key(1, apps)["applications:3"].status == "ok"
    over = by_key(2, apps)["applications:3"]
    assert (over.status, over.label) == (
        "warn",
        "3 cymoxanil applications this season, label max 2",
    )


# --- dose ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dose, status, label",
    [
        ("3.75", "ok", "Copper dose 3.75 kg/ha, label max 3.75"),
        ("4", "warn", "Copper dose 4 kg/ha, above the label max 3.75"),
    ],
)
def test_dose_against_the_label_max(dose, status, label):
    c = by_key(1, [app(1, "2024-05-09", dose=dose)])["dose:1"]
    assert (c.status, c.label) == (status, label)


def test_unknown_dose_has_no_dose_check():
    assert "dose:1" not in by_key(1, [app(1, "2024-05-09", dose=None)])


# --- PHI and minimum interval ----------------------------------------------------------------


def test_phi_says_harvest_not_before_the_latest_date():
    apps = [app(1, "2024-05-10", product=2), app(1, "2024-05-10", product=3, dose="0.4")]
    c = by_key(1, apps)["phi"]
    assert (c.status, c.label) == ("info", "Harvest not before 7 Jun")
    assert "PAJO" in c.detail and "28" in c.detail


def test_no_phi_on_the_label_means_no_phi_check():
    assert "phi" not in by_key(1, [app(1, "2024-05-10")])


def test_minimum_interval_since_the_previous_spray_of_the_product():
    apps = [
        app(1, "2024-05-01", product=3, dose="0.4"),
        app(2, "2024-05-05", product=3, dose="0.4"),
        app(3, "2024-05-12", product=3, dose="0.4"),
    ]
    assert "interval:3" not in by_key(1, apps)  # no previous spray
    short = by_key(2, apps)["interval:3"]
    assert (short.status, short.label) == (
        "warn",
        "4 days since the last cymoxanil spray, label min 7",
    )
    ok = by_key(3, apps)["interval:3"]
    assert (ok.status, ok.label) == ("ok", "7 days since the last cymoxanil spray, label min 7")


# --- one spray on several parcels ------------------------------------------------------------


def test_worst_parcel_wins_and_the_detail_names_it():
    apps = [
        app(1, "2024-05-01", parcel="P6", dose="10"),
        app(1, "2024-05-01", parcel="P6", product=2),
        app(2, "2024-05-09", parcel="P5"),
        app(2, "2024-05-09", parcel="P6"),
    ]
    c = by_key(2, apps)["copper"]
    assert c.label == "Copper 2.4 of 4 kg Cu/ha this year"
    assert c.detail.startswith("Highest on P6 (P5 0.4).")
    n = by_key(2, apps)["applications:1"]
    assert n.label == "2 of 5 copper applications this season"
    assert n.detail.startswith("Highest on P6 (P5 1).")


def test_many_parcels_name_the_tied_highest_and_a_range_for_the_rest():
    apps = [app(1, "2024-04-10", parcel=p) for p in ("P5", "P6")]
    apps += [app(2, "2024-05-09", parcel=f"P{n}") for n in range(1, 11)]
    c = by_key(2, apps)["copper"]
    assert c.label == "Copper 0.8 of 4 kg Cu/ha this year"
    assert c.detail.startswith("Highest on P5, P6 (8 other parcels 0.4).")


def test_same_value_on_every_parcel_names_no_parcel():
    apps = [app(1, "2024-05-09", parcel=p) for p in ("P5", "P6")]
    assert not by_key(1, apps)["copper"].detail.startswith("Highest")


def test_warn_beats_ok_across_parcels():
    apps = [app(1, "2024-05-01", parcel="P6", dose="20"), app(2, "2024-05-09", parcel="P5")]
    apps.append(app(2, "2024-05-09", parcel="P6"))
    assert by_key(2, apps)["copper"].status == "warn"


def test_checks_come_in_a_stable_order():
    apps = [app(1, "2024-05-10", product=1), app(1, "2024-05-10", product=2)]
    assert [c.key for c in checks.spray_checks(1, apps)] == [
        "copper",
        "applications:1",
        "applications:2",
        "dose:1",
        "dose:2",
        "phi",
    ]


# --- inputs and outputs ----------------------------------------------------------------------


def test_label_reads_product_props_and_ignores_the_rest():
    props = {
        "placeholder": False,
        "label_max_dose_kg_ha": 0.45,
        "label_max_applications": 2,
        "phi_days": 28,
        "min_interval_days": 7,
    }
    assert Label.from_props(props) == PAJO
    assert Label.from_props({"cu_fraction": 0.2}).cu_fraction == Decimal("0.2")
    assert Label.from_props(None) == Label()


@pytest.mark.parametrize(
    "value, unit, kg",
    [("2", "kg/ha", "2"), ("400", "g/ha", "0.4"), ("2", "l/ha", None), ("2", "kg", None)],
)
def test_dose_kg_ha_converts_mass_per_hectare_only(value, unit, kg):
    got = checks.dose_kg_ha(Decimal(value), unit)
    assert got == (None if kg is None else Decimal(kg))


def test_summary_is_one_short_line_with_a_word_per_status():
    apps = [app(1, "2024-05-10", product=2)]
    out = [c.as_dict() for c in checks.spray_checks(1, apps)]
    assert set(out[0]) == {"key", "status", "label", "detail"}
    assert checks.summary(out) == (
        "OK: 1 of 8 sulphur applications this season; "
        "OK: Sulphur dose 2 kg/ha, label max 12.5; "
        "Note: Harvest not before 13 May"
    )
    assert checks.summary([]) == ""


@pytest.mark.parametrize("unknown", [False, True])
def test_repeated_product_rates_are_one_dose_and_one_application(unknown):
    apps = [app(1, "2024-05-09"), app(1, "2024-05-09", dose=None if unknown else "2")]
    c = by_key(1, apps)
    assert c["dose:1"].status == "warn"
    assert c["applications:1"].label == "1 of 5 copper applications this season"
    if unknown:
        assert "incomplete" in c["dose:1"].label
        assert "at least 2" in c["dose:1"].label
    else:
        assert c["dose:1"].label == "Copper dose 4 kg/ha, above the label max 3.75"
        assert c["copper"].label == "Copper 0.8 of 4 kg Cu/ha this year"


@pytest.mark.parametrize(
    "previous, status, days", [("2023-12-29", "warn", 4), ("2023-12-25", "ok", 8)]
)
def test_interval_crosses_year_boundary_but_annual_totals_reset(previous, status, days):
    label = replace(PAJO, cu_fraction=Decimal("0.2"))
    apps = [
        replace(app(1, previous, product=3, dose="0.4"), label=label),
        replace(app(2, "2024-01-02", product=3, dose="0.4"), label=label),
    ]
    got = by_key(2, apps)
    assert got["interval:3"].status == status
    assert got["interval:3"].label == f"{days} days since the last cymoxanil spray, label min 7"
    assert got["applications:3"].label == "1 of 2 cymoxanil applications this season"
    assert got["copper"].label == "Copper 0.08 of 4 kg Cu/ha this year"


def test_split_rate_property_preserves_total_count_and_parcel_association():
    import random

    rng = random.Random(20261017)
    for _ in range(100):
        first = Decimal(rng.randrange(1, 400)) / 100
        rates = [
            app(1, "2024-05-09", dose=str(first)),
            app(1, "2024-05-09", dose=str(Decimal("4") - first)),
            app(1, "2024-05-09", parcel="P2", dose="1"),
        ]
        rng.shuffle(rates)
        got = by_key(1, rates)
        assert got["dose:1"].label == "Copper dose 4 kg/ha, above the label max 3.75"
        assert got["copper"].label == "Copper 0.8 of 4 kg Cu/ha this year"
        assert got["applications:1"].label == "1 of 5 copper applications this season"
        assert "Highest on P1 (P2 1)" in got["dose:1"].detail


@pytest.mark.parametrize("later_known", [False, True])
def test_known_copper_without_composition_keeps_total_incomplete(later_known):
    apps = [replace(app(1, "2024-05-01"), label=Label())]
    if later_known:
        apps.append(app(2, "2024-05-09"))
    got = by_key(2 if later_known else 1, apps)
    assert got["copper"].status == "warn"
    amount = "0.4" if later_known else "0"
    assert got["copper"].label == f"Copper at least {amount} kg Cu/ha this year (incomplete)"
    assert "composition" in got["copper"].detail
