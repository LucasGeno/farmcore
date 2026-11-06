"""Planning writes are owner-only; reads and corrections stay farm-scoped."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.planning.models import PlanningJob
from farmcore.core.access import DataGrant, ScopeDenied, ScopedRepository, ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm
from farmcore.core.models.identity import FarmMembership
from farmcore.main import create_app
from tests.apps.planning.conftest import NOW, job_payload
from tests.apps.share.conftest import make_parcel
from tests.core.test_member_sessions import _account, _login


def _planning_farm(factory, settings):
    with factory.begin() as session:
        farm = Farm(name="Planning farm", props={"region": "FRI12"})
        session.add(farm)
        session.flush()
        ensure_owner_grant(session, farm)
        parcel = make_parcel(session, farm, 1)
        products = ensure_products(session)
        advice = Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="spray",
            reasons=[],
            products=[],
            props={
                "assessment_status": "evaluable",
                "config_version": {"id": None, "status": "unreviewed"},
            },
        )
        session.add(advice)
        session.flush()
        set_replay(session, NOW, settings)
        farmland = (farm, parcel, products, advice)
    _account(factory, login="owner", farm_id=farmland[0].id, role="owner")
    _account(factory, login="expert", farm_id=farmland[0].id, role="advisor")
    _account(factory, login="foreign", role="owner")
    return farmland


def _headers(browser):
    return {
        "Origin": "https://testserver",
        "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
    }


def test_owner_writes_with_csrf_while_advisor_reads_only(settings, committed_session_factory):
    farm, parcel, products, advice = _planning_farm(committed_session_factory, settings)
    app = create_app(settings, session_factory=committed_session_factory)
    payload = job_payload([parcel], products, advice)
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        headers = _headers(owner)
        assert (
            owner.post(
                "/planning/jobs",
                params={"farm": farm.id},
                json=payload,
                headers={"Origin": "https://testserver"},
            ).status_code
            == 403
        )
        assert (
            owner.post(
                "/planning/jobs",
                params={"farm": farm.id},
                json=payload,
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        created = owner.post(
            "/planning/jobs", params={"farm": farm.id}, json=payload, headers=headers
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

    with TestClient(app, base_url="https://testserver") as expert:
        assert _login(expert, login="expert").status_code == 303
        expert_headers = _headers(expert)
        detail = expert.get(f"/planning/jobs/{job_id}.json", params={"farm": farm.id})
        assert detail.status_code == 200 and detail.json()["id"] == job_id
        assert (
            expert.post(
                "/planning/jobs", params={"farm": farm.id}, json=payload, headers=expert_headers
            ).status_code
            == 403
        )
        assert (
            expert.patch(
                f"/planning/jobs/{job_id}",
                params={"farm": farm.id},
                json={"expected_revision": 1, "name": "Advisor edit"},
                headers=expert_headers,
            ).status_code
            == 403
        )
        assert (
            expert.post(
                f"/planning/jobs/{job_id}/cancel",
                params={"farm": farm.id},
                json={"expected_revision": 1, "reason": "Advisor cancel"},
                headers=expert_headers,
            ).status_code
            == 403
        )

    with committed_session_factory.begin() as session:
        membership = session.scalar(
            select(FarmMembership).where(
                FarmMembership.farm_id == farm.id, FarmMembership.role == "advisor"
            )
        )
        membership.revoked_at = datetime.now(UTC)
    with TestClient(app, base_url="https://testserver") as expert:
        assert _login(expert, login="expert").status_code == 303
        denied = expert.get(f"/planning/jobs/{job_id}.json", params={"farm": farm.id})
        assert denied.status_code == 403
        assert "Spray block A" not in denied.text


def test_foreign_members_cannot_read_or_write_the_job(settings, committed_session_factory):
    farm, parcel, products, advice = _planning_farm(committed_session_factory, settings)
    app = create_app(settings, session_factory=committed_session_factory)
    payload = job_payload([parcel], products, advice)
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        created = owner.post(
            "/planning/jobs",
            params={"farm": farm.id},
            json=payload,
            headers=_headers(owner),
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

    with TestClient(app, base_url="https://testserver") as foreign:
        assert _login(foreign, login="foreign").status_code == 303
        headers = _headers(foreign)
        denied = foreign.get(f"/planning/jobs/{job_id}.json", params={"farm": farm.id})
        assert denied.status_code == 403
        assert "Spray block A" not in denied.text
        assert (
            foreign.patch(
                f"/planning/jobs/{job_id}",
                params={"farm": farm.id},
                json={"expected_revision": 1, "name": "Foreign edit"},
                headers=headers,
            ).status_code
            == 403
        )


def test_cross_farm_assessment_and_parcel_ids_are_refused(
    client, session, farm, parcels, products, advice
):
    other = Farm(name="Other planning farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    other_parcel = make_parcel(session, other, 1)
    other_advice = Advice(
        farm_id=other.id,
        parcel_id=other_parcel.id,
        evaluated_at=NOW,
        decision="spray",
        reasons=[],
        products=[],
        props={"assessment_status": "evaluable", "config_version": {"id": None, "status": "x"}},
    )
    session.add(other_advice)
    session.flush()
    created = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice),
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    assert client.get(f"/planning/jobs/{job_id}.json", params={"farm": other.id}).status_code == 404
    assert (
        client.patch(
            f"/planning/jobs/{job_id}",
            params={"farm": other.id},
            json={"expected_revision": 1, "name": "Other farm"},
        ).status_code
        == 404
    )
    foreign_assessment = client.post(
        "/planning/jobs",
        params={"farm": other.id},
        json=job_payload([other_parcel], products, other_advice, assessment_id=advice.id),
    )
    assert foreign_assessment.status_code == 422
    foreign_parcel = client.post(
        "/planning/jobs",
        params={"farm": other.id},
        json=job_payload([parcels[0]], products, other_advice, assessment_id=other_advice.id),
    )
    assert foreign_parcel.status_code == 422


def test_a_grant_without_planning_read_cannot_read_planned_jobs(session, farm):
    grant = DataGrant(farm_id=farm.id, grantee="recipient", scopes=["fields:read"], purpose="x")
    session.add(grant)
    session.flush()
    repo = ScopedRepository(session, farm.id, grant, "recipient", "test")
    with pytest.raises(ScopeDenied):
        repo.list(PlanningJob, scope="planning:read")
    grant.scopes = ["planning:read"]
    session.flush()
    assert repo.list(PlanningJob, scope="planning:read") == []
