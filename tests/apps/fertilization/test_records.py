"""Fertilization records through the draft service, on real Postgres.

Failure model (tier 3; each named failure has one owning test, here or beside):
1. A conversion is invented: a volume without a density, a per-hectare amount on a parcel of
   unknown area, or a composition read from a product name. -> `test_an_incomplete_...`
2. A composition without provenance, an amount in an unknown unit, another farm's parcel, or a
   correction of another farm's record is accepted. -> `test_a_proposal_is_refused_...`
3. Stock moves twice (retried or concurrent confirms), on a rolled-back confirm, or for planned
   or open work. -> `test_stock.py::test_stock_moves_once_...`
4. Two corrections of one record both apply, a retried correction applies twice, or the original
   values, reason and actor are lost. -> `test_stock.py::test_competing_corrections_...`
5. Farm and module authority: a visitor proposes, or a switched-off app still records.
   -> `test_routes.py`; from a process loaded before the switch (the bot) -> `test_a_process_...`
6. A legacy stock row is lost or rewritten by the schema change, or a downgrade drops a
   fertiliser's stock. -> `test_migration.py`
Not owned here (core already proves it): token actors never confirm, another principal's draft
is a 404, expired and stale-revision drafts are refused (`tests/core/test_drafts.py`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations.models import CropProtectionItem
from farmcore.core import drafts
from farmcore.core.models.canonical import CropSeason, Farm, Log
from farmcore.core.models.catalog import CatalogItem
from farmcore.core.models.kernel import AppModule
from tests.apps.fertilization.conftest import (
    APPLIED,
    LABEL,
    MANIFESTS,
    application,
    confirm,
    propose,
    seed,
    used,
)


@pytest.mark.parametrize(
    ("asked", "lacks", "answer", "expected_used"),
    [
        # The name says 27 but the composition is not read from it.
        (lambda c: {"composition": None}, ["composition"], {"composition": LABEL}, "600"),
        # 150 l/ha x 2 ha x 1.32 kg/l (the label's density) = 396 kg.
        (
            lambda c: {"amount": {"value": "150", "unit": "l/ha"}},
            ["density"],
            {"density_kg_per_l": {"value": "1.32", "source": "label"}},
            "396",
        ),
        # 300 kg/ha on a parcel whose area is unknown: the farmer gives 0.5 ha -> 150 kg.
        (lambda c: {"parcel_ids": [c.Z]}, ["area"], {"area_ha": {"value": "0.5"}}, "150"),
        # Bought in litres, applied in kg: 264 kg/ha x 2 ha / 1.32 kg/l = 400 l.
        (
            lambda c: {"amount": {"value": "264", "unit": "kg/ha"}, "stock_unit": "l"},
            ["density"],
            {"density_kg_per_l": {"value": "1.32", "source": "label"}},
            "400",
        ),
    ],
    ids=["composition", "density", "area", "litre-stock"],
)
def test_an_incomplete_application_asks_and_is_never_recorded_with_a_guess(
    session, case, asked, lacks, answer, expected_used
):
    asked = asked(case)
    if "stock_unit" in asked:
        session.execute(update(InventoryStock).values(unit=asked.pop("stock_unit")))
    payload = application(case, **asked)
    draft = propose(session, case, payload)
    assert draft.payload["missing"] == lacks
    with pytest.raises(drafts.DraftInvalid, match="incomplete"):
        confirm(session, case, draft.id)
    assert session.scalar(select(Log.id).where(Log.type == "fertilise")) is None
    assert used(session, case) == 0

    drafts.revise(
        session,
        MANIFESTS,
        farm_id=case.farm_id,
        draft_id=draft.id,
        actor="operator",
        payload={**payload, **answer},
        revision=1,
    )
    _, recorded = confirm(session, case, draft.id, revision=2)
    assert recorded
    assert used(session, case) == Decimal(expected_used)


@pytest.mark.parametrize(
    ("kind", "payload", "reason"),
    [
        ("fertilization", {"composition": {"nutrients": {"N": "27"}}}, "comes from"),
        ("fertilization", {"amount": {"value": "3", "unit": "sacks"}}, "known unit"),
        ("fertilization", {"parcel_ids": "other"}, "not on this farm"),
        ("fertilization-correction", "other-record", "not found"),
        ("fertilization", "crop-protection-item", "not in the catalogue"),
    ],
    ids=[
        "composition-without-source",
        "unknown-unit",
        "other-farm-parcel",
        "other-farm-record",
        "crop-protection-item",
    ],
)
def test_a_proposal_is_refused_with_its_reason(session, settings, case, kind, payload, reason):
    other_farm = Farm(name="Other farm")
    session.add(other_farm)
    session.flush()
    other = seed(session, settings, farm=other_farm)
    if payload == "other-record":
        theirs = propose(session, other, application(other), key="theirs")
        _, _ = confirm(session, other, theirs.id)
        payload = {"log_id": theirs.result_id, "reason": "typo", "changes": {"notes": "x"}}
    elif payload == "crop-protection-item":  # an E-Phy pesticide is not a fertiliser
        pesticide = CatalogItem(name="Copper Shield 20 WP")
        session.add(pesticide)
        session.flush()
        session.add(
            CropProtectionItem(
                catalog_item_id=pesticide.id, authorisation_no="X-1", active_substance="copper"
            )
        )
        payload = application(case, fertiliser={"name": "Copper", "catalog_item_id": pesticide.id})
    elif payload.get("parcel_ids") == "other":
        payload = application(case, parcel_ids=[other.A])
    else:
        payload = application(case, **payload)
    with pytest.raises(drafts.DraftInvalid, match=reason) as refused:
        propose(session, case, payload, kind=kind)
    assert all(
        word not in str(refused.value) for word in ("sacks", "FertilizationDraft", "https://")
    )


def test_the_season_is_the_one_covering_the_farm_local_day(session, case):
    """00:30 in Paris on a season's first day is still the previous day in UTC."""
    season = CropSeason(
        farm_id=case.farm_id, parcel_id=case.A, starts_on=date(2024, 4, 9), created_by="test"
    )
    session.add(season)
    session.flush()
    early = APPLIED.replace(hour=0, minute=30).isoformat()
    draft = propose(session, case, application(case, ts_start=early))
    log = session.get(Log, confirm(session, case, draft.id)[0].result_id)
    assert log.props["fertilization"]["seasons"] == {str(case.A): season.id}


def test_a_process_loaded_before_the_switch_off_cannot_confirm_its_draft(session, case):
    """The bot keeps the manifests it started with; the draft service reads the live state."""
    draft = propose(session, case, application(case))
    session.merge(AppModule(name="fertilization", version="0", state="uninstalled"))
    session.flush()
    with pytest.raises(drafts.DraftInvalid):
        confirm(session, case, draft.id)
    assert session.scalar(select(Log.id).where(Log.type == "fertilise")) is None
