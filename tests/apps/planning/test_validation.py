"""Invalid planned-job input is refused before any row is written."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from farmcore.apps.planning.models import PlanningJob
from farmcore.core.models.canonical import Advice
from tests.apps.planning.conftest import NOW, job_payload


def _count(session):
    session.expire_all()
    return session.scalar(select(func.count()).select_from(PlanningJob))


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"parcel_ids": []},
        {"parcel_ids": [1, 1]},
        {"parcel_ids": [0]},
        {"duration_minutes": 0},
        {"duration_minutes": 1441},
        {"scheduled_start": "2024-04-09T06:00:00"},
        {"assessment_id": 2**63 - 1},
        {"product_id": 2**63 - 1},
        {"notes": "x" * 10001},
        {"status": "cancelled"},
        {"config_version_id": 1},
    ],
)
def test_invalid_create_payload_writes_nothing(
    client, session, farm, parcels, products, advice, change
):
    payload = job_payload(parcels, products, advice, **change)
    response = client.post("/planning/jobs", params={"farm": farm.id}, json=payload)
    assert response.status_code == 422, response.text
    assert _count(session) == 0


def test_an_assessment_must_be_evaluable_and_cover_a_job_parcel(
    client, session, farm, parcels, products, advice
):
    cases = [
        {**job_payload(parcels, products, advice), "parcel_ids": [parcels[1].id]},
        {**job_payload(parcels, products, advice), "assessment_id": 2**63 - 1},
    ]
    for payload in cases:
        response = client.post("/planning/jobs", params={"farm": farm.id}, json=payload)
        assert response.status_code == 422, response.text

    limited = Advice(
        farm_id=farm.id,
        parcel_id=parcels[0].id,
        evaluated_at=NOW,
        decision="none",
        reasons=[],
        products=[],
        props={"assessment_status": "limited", "config_version": {"id": None, "status": "x"}},
    )
    session.add(limited)
    session.flush()
    response = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, limited),
    )
    assert response.status_code == 422
    assert _count(session) == 0


def test_organic_mode_withholds_a_product_without_a_source(
    client, session, farm, parcels, products, advice
):
    farm.props = {**(farm.props or {}), "production": "organic"}
    session.flush()
    response = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice, product_id=products["cymoxanil-penetrant"].id),
    )
    assert response.status_code == 422, response.text
    assert "organic" in response.text.lower()
    assert _count(session) == 0


def test_edits_cannot_change_the_frozen_assessment_or_version(
    client, farm, parcels, products, advice
):
    created = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice),
    ).json()
    for change in ({"assessment_id": 1}, {"config_version_id": 1}, {"status": "cancelled"}):
        response = client.patch(
            f"/planning/jobs/{created['id']}",
            params={"farm": farm.id},
            json={"expected_revision": 1, **change},
        )
        assert response.status_code == 422, response.text
