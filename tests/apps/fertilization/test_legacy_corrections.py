"""Corrections of pre-entered-quantity records after core_0018 moved their doses."""

import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.core.models.canonical import Log, Parcel
from farmcore.core.models.catalog import LegacyQuantityMap, TypedQuantity, enter_quantity
from farmcore.core.models.corrections import OperationCorrection
from tests.apps.fertilization.conftest import APPLIED, seed, used
from tests.apps.fertilization.test_routes import _draft_id, _form, _post


@pytest.fixture
def corrected(session, settings, client):
    case = seed(session, settings)
    log = Log(
        farm_id=case.farm_id,
        type="fertilise",
        status="done",
        name="CAN 27",
        ts_start=APPLIED,
        parcels=[session.get(Parcel, case.A)],
    )
    session.add(log)
    session.flush()
    moved = enter_quantity(
        session,
        log_id=log.id,
        value=Decimal("200"),
        unit="kg/ha",
        catalog_item_id=case.item,
        label="CAN 27",
        entered_measure="rate",
    )
    session.add(LegacyQuantityMap(quantity_id=9001, typed_quantity_id=moved.id))
    session.commit()
    farm = f"?farm={case.farm_id}"
    draft = _draft_id(
        _post(
            client,
            f"/fertilization/records/{log.id}/corrections{farm}",
            _form(case, request_key="legacy-fix", amount="180", unit="kg", reason="weighed"),
        )
    )
    for _ in range(2):
        assert _post(client, f"/drafts/{draft}/confirm{farm}", {"revision": "1"}).status_code == 303
    receipt = session.scalar(select(OperationCorrection))
    return case, log.id, moved.id, receipt.id


def test_corrected_legacy_record_and_correction_pages_render(session, client, corrected):
    case, log_id, _, receipt_id = corrected
    paths = [
        f"/fertilization/records/{log_id}",
        f"/operations/logs/{log_id}/corrections",
        f"/operations/corrections/{receipt_id}",
    ]
    with TestClient(
        client.app, headers=dict(client.headers), raise_server_exceptions=False
    ) as browser:
        responses = {path: browser.get(f"{path}?farm={case.farm_id}") for path in paths}
    assert {path: r.status_code for path, r in responses.items()} == dict.fromkeys(paths, 200)
    assert "180 kg" in responses[paths[2]].text
    assert "weighed" in responses[paths[1]].text
    assert 'name="dose_value"' not in responses[paths[1]].text  # never a spray editor


def test_corrected_amount_is_shown_and_counted_once_with_original_evidence_kept(
    session, client, corrected
):
    case, log_id, moved_id, _ = corrected
    farm = f"?farm={case.farm_id}"
    page = client.get(f"/operations/{farm}")
    assert page.status_code == 200
    assert not re.search(r"200(?:\.0+)?\s+kg/ha", page.text)
    assert "180" in page.text and "kg" in page.text
    assert used(session, case) == Decimal("180")
    nutrients = client.get(f"/fertilization/{farm}")
    assert "N 24.3 kg" in nutrients.text  # 180 kg / 2 ha * 27% N
    assert "180 kg" in client.get(f"/fertilization/map/parcels/{case.A}{farm}").text
    session.expire_all()
    moved = session.get(TypedQuantity, moved_id)
    assert (moved.entered_value, moved.entered_unit, moved.entered_measure) == (
        Decimal("200"),
        "kg/ha",
        "rate",
    )
    assert session.get(LegacyQuantityMap, 9001).typed_quantity_id == moved_id
    assert (
        len(session.scalars(select(TypedQuantity).where(TypedQuantity.log_id == log_id)).all()) == 2
    )

    # Multiple entered amounts must all survive the current-summary projection.
    enter_quantity(
        session,
        log_id=log_id,
        value=Decimal("2.50"),
        unit="kg",
        catalog_item_id=case.item,
        label="CAN 27",
    )
    session.commit()
    page = client.get(f"/operations/{farm}")
    line = re.search(rf'<li[^>]*data-record="{log_id}">.*?</li>', page.text, re.S)[0]
    assert "180 kg" in line and "2.50 kg" in line
    assert not re.search(r"200(?:\.0+)?\s+kg/ha", line)


def test_post92_fertilization_index_line_is_unchanged(session, settings, client):
    case = seed(session, settings)
    log = Log(
        farm_id=case.farm_id,
        type="fertilise",
        status="done",
        name="CAN 27",
        ts_start=APPLIED,
        parcels=[session.get(Parcel, case.A)],
    )
    session.add(log)
    session.commit()

    def index_line():
        response = client.get(f"/operations/?farm={case.farm_id}")
        assert response.status_code == 200
        return re.search(rf'<li[^>]*data-record="{log.id}">.*?</li>', response.text, re.S)[0]

    # Main ignores entered-only amounts in this summary: keep the complete record line intact.
    before = index_line()
    enter_quantity(
        session,
        log_id=log.id,
        value=Decimal("180"),
        unit="kg",
        catalog_item_id=case.item,
        label="CAN 27",
    )
    session.commit()
    assert index_line() == before
