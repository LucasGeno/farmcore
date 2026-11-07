"""State is in PostgreSQL: a new app instance and a second client see the same plan."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.planning.models import PlanningJob
from farmcore.core.access import ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm
from farmcore.main import create_app
from tests.apps.planning.conftest import NOW, job_payload
from tests.apps.share.conftest import make_parcel


def test_a_planned_job_and_availability_survive_a_new_app_instance(
    settings, committed_session_factory
):
    with committed_session_factory.begin() as session:
        farm = Farm(name="Persistent planning farm", props={"region": "FRI12"})
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
            props={"assessment_status": "evaluable", "config_version": {"id": None, "status": "x"}},
        )
        session.add(advice)
        set_replay(session, NOW, settings)
        session.flush()
        farm_id = farm.id
        payload = job_payload([parcel], products, advice)

    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    first = TestClient(
        create_app(settings, session_factory=committed_session_factory), headers=headers
    )
    with first:
        created = first.post("/planning/jobs", params={"farm": farm_id}, json=payload)
        assert created.status_code == 201
        job_id = created.json()["id"]
    first.close()

    with committed_session_factory() as session:
        assert session.get(PlanningJob, job_id).name == "Spray block A"

    second = TestClient(
        create_app(settings, session_factory=committed_session_factory), headers=headers
    )
    with second:
        detail = second.get(f"/planning/jobs/{job_id}.json", params={"farm": farm_id})
        assert detail.status_code == 200 and detail.json()["revision"] == 1
        assert (
            second.post(
                "/forecast/busy",
                params={"farm": farm_id},
                data={
                    "farm": str(farm_id),
                    "day": "2024-04-09",
                    "busy": "1",
                    "next": "/forecast/plan",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
    second.close()

    third = TestClient(
        create_app(settings, session_factory=committed_session_factory), headers=headers
    )
    with third:
        detail = third.get(f"/planning/jobs/{job_id}.json", params={"farm": farm_id})
        assert detail.json()["availability"] == {"day": "2024-04-09", "available": False}
    third.close()
    with committed_session_factory() as session:
        assert session.scalar(select(PlanningJob.id)) == job_id
