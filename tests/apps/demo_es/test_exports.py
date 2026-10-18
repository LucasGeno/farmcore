from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.core import clock, logs
from farmcore.core.models.canonical import Parcel

pytestmark = [pytest.mark.data, pytest.mark.spanish_data]


def test_sigpac_identity_exports_without_inventing_bordeaux_growth_stages(
    client, session, settings, spanish
):
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == spanish.id))
    now = datetime(2024, 4, 10, 8, tzinfo=UTC)
    logs.record_operation(
        session,
        farm_id=spanish.id,
        type="spray",
        status="done",
        parcel_ids=[parcel.id],
        ts_start=now,
        ts_end=None,
        quantities=[],
        actor="test",
        occurred_at=now,
        source=logs.SourceInput(
            "file", "review-demo-es", {"demo_data": True}, None, logs.RANK_FILE
        ),
    )
    clock.set_replay(session, now, settings)
    session.commit()
    response = client.get("/operations/register.json", params={"farm": spanish.id})
    assert response.status_code == 200
    (row,) = response.json()
    assert row["iacs_parcel_id"] == parcel.iacs_id
    assert row["parcel_aliases"] == [f"sigpac-2024:{parcel.iacs_id}"]
    assert row["bbch"] is None and row["bbch_source"] == "none"
    entities = client.get("/share/ngsi-ld.json", params={"farm": spanish.id}).json()
    exported = next(e for e in entities if e["type"] == "AgriParcel")
    assert {
        "application": "urn:ngsi-ld:AgriApp:sigpac-2024",
        "applicationEntityId": parcel.iacs_id,
    } in exported["relatedSource"]
