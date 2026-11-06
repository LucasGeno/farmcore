"""Planned jobs are persistent intended work with revisions, never completed records."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from farmcore.apps.forecast.models import ForecastAvailability
from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.inventory.service import stock_rows
from farmcore.apps.planning.models import PlanningJob, PlanningJobParcel, PlanningJobRevision
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm, Log
from tests.apps.planning.conftest import NOW, job_payload


def _created(client, farm, parcels, products, advice, **overrides):
    response = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_edit_and_cancel_keep_a_revision_history(
    client, session, farm, parcels, products, advice
):
    created = _created(client, farm, parcels, products, advice)

    assert created["farm_id"] == farm.id
    assert created["status"] == "planned"
    assert created["revision"] == 1
    assert created["name"] == "Spray block A"
    assert [p["id"] for p in created["parcels"]] == [parcels[0].id]
    assert created["assessment"]["id"] == advice.id
    assert created["assessment"]["parcel_id"] == parcels[0].id
    assert created["assessment"]["decision"] == "spray"
    assert created["assessment"]["config_version"] == {
        "id": None,
        "status": "unreviewed",
        "accuracy_claim": False,
    }
    assert created["product"]["id"] == products["copper-contact"].id
    assert created["duration_minutes"] == 90
    assert created["scheduled_end"].startswith("2024-04-09T07:30:00")
    assert created["availability"] == {"day": "2024-04-09", "available": True}
    assert created["created_by"] == "owner"
    assert created["cancelled_at"] is None

    session.expire_all()
    job = session.get(PlanningJob, created["id"])
    assert (job.farm_id, job.status, job.revision) == (farm.id, "planned", 1)
    assert session.scalars(
        select(PlanningJobParcel).where(PlanningJobParcel.job_id == job.id)
    ).all() == [session.get(PlanningJobParcel, (job.id, parcels[0].id))]
    revisions = session.scalars(
        select(PlanningJobRevision)
        .where(PlanningJobRevision.job_id == job.id)
        .order_by(PlanningJobRevision.revision)
    ).all()
    assert [(r.revision, r.action, r.status) for r in revisions] == [(1, "created", "planned")]
    assert revisions[0].snapshot["availability"] == {"day": "2024-04-09", "available": True}

    edited = client.patch(
        f"/planning/jobs/{job.id}",
        params={"farm": farm.id},
        json={
            "expected_revision": 1,
            "name": "Spray block A (evening)",
            "duration_minutes": 120,
            "notes": "Moved to the calmer window",
        },
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["revision"], body["name"], body["duration_minutes"]) == (
        2,
        "Spray block A (evening)",
        120,
    )
    assert body["assessment"]["id"] == advice.id  # assessment/version stay frozen

    cancelled = client.post(
        f"/planning/jobs/{job.id}/cancel",
        params={"farm": farm.id},
        json={"expected_revision": 2, "reason": "Weather changed"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["revision"] == 3
    assert cancelled.json()["cancelled_by"] == "owner"
    assert cancelled.json()["cancelled_at"] is not None

    # A cancelled job is terminal and stays visible.
    assert (
        client.patch(
            f"/planning/jobs/{job.id}",
            params={"farm": farm.id},
            json={"expected_revision": 3, "name": "Again"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/planning/jobs/{job.id}/cancel",
            params={"farm": farm.id},
            json={"expected_revision": 3, "reason": "Again"},
        ).status_code
        == 409
    )
    detail = client.get(f"/planning/jobs/{job.id}.json", params={"farm": farm.id})
    assert detail.status_code == 200 and detail.json()["status"] == "cancelled"
    session.expire_all()
    assert session.get(PlanningJob, job.id).revision == 3
    revisions = session.scalars(
        select(PlanningJobRevision)
        .where(PlanningJobRevision.job_id == job.id)
        .order_by(PlanningJobRevision.revision)
    ).all()
    assert [(r.revision, r.action, r.status) for r in revisions] == [
        (1, "created", "planned"),
        (2, "edited", "planned"),
        (3, "cancelled", "cancelled"),
    ]
    assert revisions[2].reason == "Weather changed"
    assert revisions[2].snapshot["status"] == "cancelled"


def test_a_stale_revision_returns_conflict_and_writes_nothing(
    client, session, farm, parcels, products, advice
):
    created = _created(client, farm, parcels, products, advice)
    job_id = created["id"]
    stale = client.patch(
        f"/planning/jobs/{job_id}",
        params={"farm": farm.id},
        json={"expected_revision": 2, "name": "Stale edit"},
    )
    assert stale.status_code == 409
    session.expire_all()
    assert session.get(PlanningJob, job_id).name == "Spray block A"
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == job_id)
        )
        == 1
    )

    accepted = client.patch(
        f"/planning/jobs/{job_id}",
        params={"farm": farm.id},
        json={"expected_revision": 1, "name": "First edit"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["revision"] == 2
    repeated = client.patch(
        f"/planning/jobs/{job_id}",
        params={"farm": farm.id},
        json={"expected_revision": 1, "name": "Repeated stale edit"},
    )
    assert repeated.status_code == 409
    session.expire_all()
    assert session.get(PlanningJob, job_id).name == "First edit"
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == job_id)
        )
        == 2
    )


def test_planned_work_never_becomes_a_log_or_moves_stock(
    client, session, farm, parcels, products, advice
):
    stock = InventoryStock(
        farm_id=farm.id,
        product_id=products["copper-contact"].id,
        season=NOW.year,
        unit="kg",
        purchased=Decimal("10"),
    )
    session.add(stock)
    session.flush()
    _created(client, farm, parcels, products, advice)

    assert session.scalar(select(func.count()).select_from(Log)) == 0
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")
    rows = stock_rows(repo, now=NOW, tz="Europe/Paris")
    assert rows[0].on_hand == Decimal("10")
    assert rows[0].sprays == 0

    register = client.get("/operations/register", params={"farm": farm.id})
    assert register.status_code == 200
    assert "Spray block A" not in register.text
    diary = client.get("/operations/", params={"farm": farm.id})
    assert diary.status_code == 200
    assert "Spray block A" not in diary.text
    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert "Spray block A" in page.text and "planned" in page.text


def test_a_job_freezes_the_assessment_parameter_version(
    client, session, farm, parcels, products, advice
):
    from farmcore.apps.review.models import ParameterVersion

    version = ParameterVersion(
        farm_id=farm.id,
        crop="vine",
        region="FRI12",
        evaluation_year=2024,
        parent_version_id=None,
        created_by="test",
        evidence="Reviewed trial",
        overrides={},
        parameter_catalog={},
        effective_config={},
        config_hash="a" * 64,
        baseline=False,
    )
    session.add(version)
    session.flush()
    advice.props = {
        **(advice.props or {}),
        "config_version": {"id": version.id, "status": "approved"},
    }
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    assert created["assessment"]["config_version"] == {
        "id": version.id,
        "status": "approved",
        "accuracy_claim": False,
    }
    edited = client.patch(
        f"/planning/jobs/{created['id']}",
        params={"farm": farm.id},
        json={"expected_revision": 1, "name": "Version stays frozen"},
    )
    assert edited.status_code == 200
    assert edited.json()["assessment"]["config_version"]["id"] == version.id

    other = Farm(name="Foreign version farm")
    session.add(other)
    session.flush()
    foreign = ParameterVersion(
        farm_id=other.id,
        crop="vine",
        region="FRI12",
        evaluation_year=2024,
        parent_version_id=None,
        created_by="test",
        evidence="Other farm trial",
        overrides={},
        parameter_catalog={},
        effective_config={},
        config_hash="b" * 64,
        baseline=False,
    )
    session.add(foreign)
    session.flush()
    advice.props = {
        **(advice.props or {}),
        "config_version": {"id": foreign.id, "status": "approved"},
    }
    session.flush()
    refused = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice),
    )
    assert refused.status_code == 422


def test_a_job_records_the_farmer_availability_at_each_revision(
    client, session, farm, parcels, products, advice
):
    session.add(
        ForecastAvailability(
            farm_id=farm.id,
            day=date(2024, 4, 9),
            available=False,
            notes="Market day",
            created_by="owner",
            updated_by="owner",
        )
    )
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    assert created["availability"] == {"day": "2024-04-09", "available": False}

    response = client.post(
        "/forecast/busy",
        params={"day": "2024-04-09", "busy": "0", "next": "/planning/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(f"/planning/jobs/{created['id']}.json", params={"farm": farm.id})
    assert detail.json()["availability"] == {"day": "2024-04-09", "available": True}
    session.expire_all()
    revision = session.scalar(
        select(PlanningJobRevision).where(PlanningJobRevision.job_id == created["id"])
    )
    assert revision.snapshot["availability"] == {"day": "2024-04-09", "available": False}


def test_the_planning_form_creates_and_cancels_a_job(
    client, session, farm, parcels, products, advice
):
    response = client.post(
        "/planning/jobs-form",
        params={"farm": farm.id},
        data={
            "name": "Form planned spray",
            "parcel_ids": [str(parcels[0].id)],
            "assessment_id": str(advice.id),
            "product_id": str(products["copper-contact"].id),
            "scheduled_start": "2024-04-09T08:00",
            "duration_minutes": "90",
            "notes": "From the planning form",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/planning/?farm={farm.id}"
    session.expire_all()
    job = session.scalar(select(PlanningJob).where(PlanningJob.farm_id == farm.id))
    assert job.name == "Form planned spray"
    assert job.scheduled_start.hour == 6  # 08:00 Europe/Paris in UTC

    cancelled = client.post(
        f"/planning/jobs/{job.id}/cancel-form",
        params={"farm": farm.id},
        data={"expected_revision": "1", "reason": "Form cancellation"},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    session.expire_all()
    assert session.get(PlanningJob, job.id).status == "cancelled"


def test_demo_reset_removes_planned_jobs_before_deleting_their_assessments(
    client, session, farm, parcels, products, advice
):
    from farmcore.core import demo_control, registry
    from farmcore.core.models.canonical import Advice

    created = _created(client, farm, parcels, products, advice)
    session.expire_all()
    demo_control._clear(session, farm, registry.discover())
    session.flush()
    session.expire_all()
    assert session.get(PlanningJob, created["id"]) is None
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.farm_id == farm.id)
        )
        == 0
    )
    assert (
        session.scalar(select(func.count()).select_from(Advice).where(Advice.farm_id == farm.id))
        == 0
    )
