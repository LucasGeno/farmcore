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
   -> `test_routes.py`
6. A legacy stock row is lost or rewritten by the schema change, or a downgrade drops a
   fertiliser's stock. -> `test_migration.py`
Not owned here (core already proves it): token actors never confirm, another principal's draft
is a 404, expired and stale-revision drafts are refused (`tests/core/test_drafts.py`).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.core import drafts
from farmcore.core.models.canonical import Farm, Log
from tests.apps.fertilization.conftest import (
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
    ],
    ids=["composition", "density", "area"],
)
def test_an_incomplete_application_asks_and_is_never_recorded_with_a_guess(
    session, case, asked, lacks, answer, expected_used
):
    payload = application(case, **asked(case))
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
    ],
    ids=["composition-without-source", "unknown-unit", "other-farm-parcel", "other-farm-record"],
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
    elif payload.get("parcel_ids") == "other":
        payload = application(case, parcel_ids=[other.A])
    else:
        payload = application(case, **payload)
    with pytest.raises(drafts.DraftInvalid, match=reason):
        propose(session, case, payload, kind=kind)
