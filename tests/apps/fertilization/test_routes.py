"""HTTP entry points: the owner's form to a reviewed record, and farm and module authority
(failure model 5 in `test_records.py`)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.core.models.canonical import Log
from farmcore.core.models.corrections import OperationCorrection
from farmcore.main import create_app
from tests.apps.fertilization.conftest import seed


def _form(case, **changes):
    form = {
        "request_key": "form-1",
        "parcel_ids": [str(case.A)],
        "applied_at": "2024-04-09T08:00",
        "fertiliser": "CAN 27",
        "catalog_item_id": str(case.item),
        "amount": "300",
        "unit": "kg/ha",
        "nutrient_N": "27",
        "source": "label",
    }
    form.update(changes)
    return form


def _post(client, url, data):
    return client.post(url, data=data, follow_redirects=False)


def _draft_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(re.search(r"/drafts/(\d+)", response.headers["location"])[1])


def test_the_owner_records_through_the_form_and_a_visitor_cannot(session, settings, client):
    case = seed(session, settings)
    session.commit()
    farm = f"?farm={case.farm_id}"
    with TestClient(create_app(settings, session_factory=client.app.state.session_factory)) as anon:
        assert anon.post(f"/fertilization/drafts{farm}", data=_form(case)).status_code == 403

    draft = _draft_id(_post(client, f"/fertilization/drafts{farm}", _form(case)))
    review = client.get(f"/fertilization/drafts/review{farm}&draft={draft}")
    assert "CAN 27" in review.text and "Still needs" not in review.text
    assert "Crop season: not recorded" in review.text  # no season on that day: shown, not guessed
    confirmed = _post(client, f"/drafts/{draft}/confirm{farm}", {"revision": "1"})
    assert confirmed.status_code == 303

    page = client.get(f"/fertilization/{farm}")
    assert "N 81 kg" in page.text  # 300 kg/ha x 27 % N
    assert "On hand: 400 kg" in client.get(f"/inventory/{farm}").text  # 1000 - 300 x 2 ha
    (log_id,) = session.scalars(select(Log.id).where(Log.type == "fertilise"))
    section = client.get(f"/fertilization/map/parcels/{case.A}{farm}")
    assert "CAN 27 300 kg/ha" in section.text

    corrections = f"/fertilization/records/{log_id}/corrections{farm}"
    unchanged = _post(client, corrections, _form(case, request_key="same", reason="checked"))
    assert unchanged.status_code == 422  # the record form posted as it is corrects nothing
    assert session.scalar(select(OperationCorrection.id)) is None

    fix = _form(case, request_key="fix-1", amount="200", reason="spreader recalibrated")
    correction = _draft_id(_post(client, corrections, fix))
    assert (
        _post(client, f"/drafts/{correction}/confirm{farm}", {"revision": "1"}).status_code == 303
    )
    (receipt,) = session.scalars(select(OperationCorrection))
    assert (receipt.reason, receipt.before_snapshot["amount"]["value"]) == (
        "spreader recalibrated",
        "300",
    )


def test_a_switched_off_app_refuses_every_entry_point(session, settings, client):
    case = seed(session, settings)
    session.commit()
    farm = f"?farm={case.farm_id}"
    draft = _draft_id(_post(client, f"/fertilization/drafts{farm}", _form(case)))
    assert _post(client, "/apps/fertilization/uninstall", {}).status_code == 200

    assert client.get(f"/fertilization/{farm}").status_code == 404
    assert client.post(f"/fertilization/drafts{farm}", data=_form(case)).status_code == 404
    refused = client.post(f"/drafts/{draft}/confirm{farm}", data={"revision": "1"})
    assert refused.status_code == 422
    assert session.scalar(select(Log.id).where(Log.type == "fertilise")) is None
