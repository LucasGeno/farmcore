"""Concurrent job edits serialize on the farm lock and one revision wins."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.planning.models import PlanningJob, PlanningJobRevision
from farmcore.core.access import ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm
from farmcore.main import create_app
from tests.apps.planning.conftest import NOW, job_payload
from tests.apps.share.conftest import make_parcel


def test_two_edits_with_the_same_revision_have_one_winner(settings, committed_session_factory):
    with committed_session_factory.begin() as session:
        farm = Farm(name="Concurrent planning farm", props={"region": "FRI12"})
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
        session.flush()
        set_replay(session, NOW, settings)
        farm_id = farm.id
        payload = job_payload([parcel], products, advice)

    app = create_app(settings, session_factory=committed_session_factory)
    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    with TestClient(app, headers=headers) as browser:
        created = browser.post("/planning/jobs", params={"farm": farm_id}, json=payload)
        assert created.status_code == 201
        job_id = created.json()["id"]

    def edit(name):
        with TestClient(app, headers=headers) as browser:
            return browser.patch(
                f"/planning/jobs/{job_id}",
                params={"farm": farm_id},
                json={"expected_revision": 1, "name": name},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(edit, ("Concurrent A", "Concurrent B")))

    assert sorted(r.status_code for r in responses) == [200, 409]
    with committed_session_factory() as session:
        job = session.get(PlanningJob, job_id)
        assert job.revision == 2
        assert job.name in {"Concurrent A", "Concurrent B"}
        revisions = session.scalars(
            select(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == job_id)
            .order_by(PlanningJobRevision.revision)
        ).all()
        assert [(r.revision, r.action) for r in revisions] == [(1, "created"), (2, "edited")]
        assert (
            session.scalar(
                select(func.count())
                .select_from(PlanningJobRevision)
                .where(PlanningJobRevision.job_id == job_id)
            )
            == 2
        )
