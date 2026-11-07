"""Planning writes are owner-only; reads and corrections stay farm-scoped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def _completion(job):
    return {
        "expected_revision": job["revision"],
        "request_key": "authorization-completion-key",
        "actual_start": (NOW - timedelta(hours=2)).isoformat(),
        "duration_minutes": 60,
        "parcel_ids": [p["id"] for p in job["parcels"]],
        "quantities": [{"product_id": job["product"]["id"], "value": "1.5", "unit": "kg/ha"}],
        "notes": "",
    }


def test_only_the_owner_completes_a_planned_job(settings, committed_session_factory):
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
        job = created.json()

    with TestClient(app, base_url="https://testserver") as expert:
        assert _login(expert, login="expert").status_code == 303
        assert (
            expert.post(
                f"/planning/jobs/{job['id']}/complete",
                params={"farm": farm.id},
                json=_completion(job),
                headers=_headers(expert),
            ).status_code
            == 403
        )

    with TestClient(app, base_url="https://testserver") as foreign:
        assert _login(foreign, login="foreign").status_code == 303
        assert (
            foreign.post(
                f"/planning/jobs/{job['id']}/complete",
                params={"farm": farm.id},
                json=_completion(job),
                headers=_headers(foreign),
            ).status_code
            == 403
        )

    with TestClient(app, base_url="https://testserver") as anonymous:
        assert (
            anonymous.post(
                f"/planning/jobs/{job['id']}/complete",
                params={"farm": farm.id},
                json=_completion(job),
                headers={"Origin": "https://testserver"},
            ).status_code
            == 403
        )

    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        done = owner.post(
            f"/planning/jobs/{job['id']}/complete",
            params={"farm": farm.id},
            json=_completion(job),
            headers=_headers(owner),
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"

    with TestClient(app, base_url="https://testserver") as expert:
        assert _login(expert, login="expert").status_code == 303
        detail = expert.get(f"/planning/jobs/{job['id']}.json", params={"farm": farm.id})
        assert detail.status_code == 200 and detail.json()["status"] == "completed"


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
    assert (
        client.post(
            f"/planning/jobs/{job_id}/complete",
            params={"farm": other.id},
            json={
                "expected_revision": 1,
                "request_key": "foreign-completion",
                "actual_start": NOW.isoformat(),
                "duration_minutes": 90,
                "parcel_ids": [other_parcel.id],
                "quantities": [
                    {
                        "product_id": products["copper-contact"].id,
                        "value": "1.5",
                        "unit": "kg/ha",
                    }
                ],
                "notes": "",
            },
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


def test_anonymous_demo_visitors_cannot_read_planned_jobs(settings, committed_session_factory):
    farm, parcel, products, advice = _planning_farm(committed_session_factory, settings)
    app = create_app(settings, session_factory=committed_session_factory)
    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    with TestClient(app, headers=headers) as owner:
        created = owner.post(
            "/planning/jobs",
            params={"farm": farm.id},
            json=job_payload([parcel], products, advice, notes="Anonymous secret note"),
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

    demo = create_app(
        settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm.id]}),
        session_factory=committed_session_factory,
    )
    with TestClient(demo) as anonymous:
        for path in (
            "/planning/",
            "/planning/jobs.json",
            f"/planning/jobs/{job_id}.json",
            "/planning/new",
        ):
            response = anonymous.get(path, params={"farm": farm.id})
            assert response.status_code == 403, (path, response.status_code)
            assert "Spray block A" not in response.text
            assert "Anonymous secret note" not in response.text
            assert "created_by" not in response.text


def test_advisors_read_resources_but_cannot_configure_or_book_them(
    settings, committed_session_factory
):
    from farmcore.apps.planning.models import PlanningMachine

    farm, parcel, products, advice = _planning_farm(committed_session_factory, settings)
    with committed_session_factory.begin() as session:
        machine = PlanningMachine(
            farm_id=farm.id,
            name="Owner sprayer",
            kind="sprayer",
            active=True,
            notes="",
            created_by="owner",
            updated_by="owner",
        )
        session.add(machine)
        session.flush()
        machine_id = machine.id
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

    with TestClient(app, base_url="https://testserver") as expert:
        assert _login(expert, login="expert").status_code == 303
        headers = _headers(expert)
        page = expert.get("/planning/", params={"farm": farm.id})
        assert page.status_code == 200
        assert "Owner sprayer" in page.text
        assert (
            expert.post(
                "/planning/resources/machines-form",
                params={"farm": farm.id},
                data={"name": "Advisor sprayer", "kind": "sprayer"},
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            expert.post(
                f"/planning/jobs/{job_id}/assignments",
                params={"farm": farm.id},
                json={"expected_revision": 1, "kind": "machine", "resource_id": machine_id},
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            expert.post(
                f"/planning/jobs/{job_id}/assign-form",
                params={"farm": farm.id},
                data={"expected_revision": "1", "machine_id": str(machine_id)},
                headers=headers,
            ).status_code
            == 403
        )
