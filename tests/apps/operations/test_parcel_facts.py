"""`parcel_facts`, the pure per-parcel spray projection behind the map panel.

F1 first: a spray log is this parcel's own only when it links this parcel alone; a log shared
with other parcels keeps its date and count, never its products, dose or copper. Values are
synthetic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from farmcore.apps.operations.parcel_facts import (
    COPPER,
    NOT_COPPER,
    UNIDENTIFIED,
    ProductLine,
    SprayLog,
    copper_this_year,
    parcel_facts,
)

NOW = datetime(2024, 6, 1, 12, tzinfo=UTC)
TODAY = date(2024, 6, 1)

COPPER_2 = ProductLine("Copper A", "2", "kg/ha", COPPER, Decimal("0.4"))
COPPER_NO_FRACTION = ProductLine("Copper B", "2", "kg/ha", COPPER, None)
COPPER_NO_DOSE = ProductLine("Copper A", "", "", COPPER, None)
SULPHUR = ProductLine("Sulphur", "5", "kg/ha", NOT_COPPER)
UNKNOWN = ProductLine("something", "2", "kg/ha", UNIDENTIFIED)


def log(log_id: int, day: str, parcels: tuple[str, ...], *products: ProductLine) -> SprayLog:
    d = date.fromisoformat(day)
    return SprayLog(log_id, datetime(d.year, d.month, d.day, 8, tzinfo=UTC), d, parcels, products)


def test_no_spray_yet() -> None:
    facts = parcel_facts("P1", [], NOW, TODAY)
    assert facts.last is None
    assert facts.season_sprays == 0
    assert facts.copper is not None and facts.copper.text == "no copper recorded"


def test_own_log_gives_products_dose_and_exact_copper() -> None:
    facts = parcel_facts("P1", [log(1, "2024-05-09", ("P1",), COPPER_2)], NOW, TODAY)
    assert facts.last is not None and facts.last.log_id == 1
    assert facts.attributable
    assert [p.text for p in facts.last_products] == ["Copper A 2 kg/ha"]
    assert facts.copper.text == "0.4 kg Cu/ha"
    assert facts.season_sprays == 1


def test_shared_log_keeps_date_and_count_but_never_dose_or_copper() -> None:
    shared = log(7, "2024-04-10", ("P91", "P92"), COPPER_2, SULPHUR)
    facts = parcel_facts("P91", [shared], NOW, TODAY)
    assert facts.last is not None and facts.last.day == date(2024, 4, 10)
    assert facts.season_sprays == 1
    assert not facts.attributable
    assert facts.last_products == ()
    assert facts.shared_with == ("P92",)
    assert facts.dose_text == "dose for this parcel unknown (logged with P92)"
    # The log's global copper dose is never this parcel's: no number at all.
    assert facts.copper.text == "copper unknown"
    assert "at least 0" not in facts.copper.text


def test_shared_log_beside_an_own_copper_spray_is_a_lower_bound() -> None:
    logs_ = [
        log(5, "2024-04-02", ("P92",), COPPER_2),
        log(7, "2024-04-10", ("P91", "P92"), COPPER_2, SULPHUR),
    ]
    p91 = parcel_facts("P91", [lg for lg in logs_ if "P91" in lg.parcels], NOW, TODAY)
    p92 = parcel_facts("P92", logs_, NOW, TODAY)
    assert p91.copper.text == "copper unknown"
    assert p92.copper.text == "at least 0.4 kg Cu/ha, incomplete"
    assert p92.season_sprays == 2
    assert p92.dose_text == "dose for this parcel unknown (logged with P91)"


def test_unknown_copper_fraction_or_dose() -> None:
    only = [log(1, "2024-05-01", ("P1",), COPPER_NO_FRACTION)]
    assert copper_this_year("P1", only, TODAY).text == "copper unknown"
    mixed = [
        log(1, "2024-04-01", ("P1",), COPPER_NO_DOSE),
        log(2, "2024-05-01", ("P1",), COPPER_2),
    ]
    assert copper_this_year("P1", mixed, TODAY).text == "at least 0.4 kg Cu/ha, incomplete"


def test_sulphur_only_says_no_copper_and_unidentified_makes_no_claim() -> None:
    assert copper_this_year("P1", [log(1, "2024-05-01", ("P1",), SULPHUR)], TODAY).text == (
        "no copper recorded"
    )
    assert copper_this_year("P1", [log(1, "2024-05-01", ("P1",), UNKNOWN)], TODAY) is None
    # An unidentified product beside a known copper spray cannot certify an exact total.
    both = [log(1, "2024-05-01", ("P1",), UNKNOWN), log(2, "2024-05-02", ("P1",), COPPER_2)]
    assert copper_this_year("P1", both, TODAY).text == "at least 0.4 kg Cu/ha, incomplete"


def test_shared_log_counts_as_unknown_whatever_its_global_products() -> None:
    # The core merge replaces a shared log's quantities: its sulphur proves nothing per parcel.
    shared = log(7, "2024-05-10", ("P91", "P92"), SULPHUR)
    assert copper_this_year("P91", [shared], TODAY).text == "copper unknown"
    own = log(5, "2024-05-02", ("P92",), COPPER_2)
    assert copper_this_year("P92", [own, shared], TODAY).text == (
        "at least 0.4 kg Cu/ha, incomplete"
    )
    empty = log(8, "2024-05-11", ("P91", "P92"))
    assert copper_this_year("P91", [empty], TODAY).text == "copper unknown"


def test_a_confirmed_log_without_products_never_says_no_copper() -> None:
    empty = log(1, "2024-05-01", ("P1",))
    assert copper_this_year("P1", [empty], TODAY) is None
    with_copper = [empty, log(2, "2024-05-02", ("P1",), COPPER_2)]
    assert copper_this_year("P1", with_copper, TODAY).text == ("at least 0.4 kg Cu/ha, incomplete")


def test_known_zero_beside_unknown_copper_is_unknown_never_at_least_0() -> None:
    zero = ProductLine("Copper A", "0", "kg/ha", COPPER, Decimal("0"))
    logs_ = [log(1, "2024-05-01", ("P1",), zero), log(2, "2024-05-02", ("P1",), COPPER_NO_DOSE)]
    assert copper_this_year("P1", logs_, TODAY).text == "copper unknown"
    assert copper_this_year("P1", logs_[:1], TODAY).text == "0 kg Cu/ha"


def test_season_is_the_calendar_year_and_the_clock_bounds_everything() -> None:
    logs_ = [
        log(1, "2023-09-01", ("P1",), COPPER_2),
        log(2, "2024-05-01", ("P1",), COPPER_2),
        log(3, "2024-07-01", ("P1",), COPPER_2),  # after the clock
    ]
    facts = parcel_facts("P1", logs_, NOW, TODAY)
    assert facts.season_sprays == 1
    assert facts.last.log_id == 2
    assert facts.copper.text == "0.4 kg Cu/ha"


def test_last_spray_differs_per_parcel_never_a_union() -> None:
    logs_ = [
        log(1, "2024-05-01", ("P1",), COPPER_2),
        log(2, "2024-05-01", ("P2",), SULPHUR),
    ]
    p1 = parcel_facts("P1", [logs_[0]], NOW, TODAY)
    p2 = parcel_facts("P2", [logs_[1]], NOW, TODAY)
    assert [p.name for p in p1.last_products] == ["Copper A"]
    assert [p.name for p in p2.last_products] == ["Sulphur"]
    assert p2.copper.text == "no copper recorded"


def test_own_or_missing_last_spray_has_no_shared_dose_line() -> None:
    own = parcel_facts("P1", [log(1, "2024-05-01", ("P1",), COPPER_NO_DOSE)], NOW, TODAY)
    assert own.shared_with == () and own.dose_text == ""
    assert [p.text for p in own.last_products] == ["Copper A (dose unknown)"]
    none = parcel_facts("P1", [], NOW, TODAY)
    assert none.shared_with == () and none.dose_text == "" and none.last_products == ()
