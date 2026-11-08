"""Concurrent job edits serialize on the farm lock and one revision wins."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.planning import resources, service
from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobAssignment,
    PlanningJobRevision,
)
from farmcore.core.access import ScopedRepository, ensure_owner_grant
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


class _PrecheckedRepo(ScopedRepository):
    """A repository that pauses once the booking pre-check has read no conflict."""

    def __init__(self, *args, ready, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready = ready
        self.release = release
        self.assignment_reads = 0

    def list(self, model, *criteria, **kwargs):
        rows = super().list(model, *criteria, **kwargs)
        if model is PlanningJobAssignment:
            self.assignment_reads += 1
            if self.assignment_reads == 2:  # the _booking_conflict query
                self.ready.set()
                assert self.release.wait(timeout=10)
        return rows


def test_two_sessions_past_the_pre_check_hit_the_database_overlap_guard(
    settings, committed_session_factory
):
    with committed_session_factory.begin() as session:
        farm = Farm(name="Overlap race farm", props={"region": "FRI12"})
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
        repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "t", "t")
        machine = resources.create_machine(
            repo, NOW, name="Overlap sprayer", kind="sprayer", work_rate_ha_per_hour="2"
        )
        first = service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="First booking",
            parcel_ids=[parcel.id],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW + timedelta(hours=2),
            duration_minutes=90,
        )
        second = service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="Second booking",
            parcel_ids=[parcel.id],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW + timedelta(hours=2),
            duration_minutes=90,
        )
        farm_id = farm.id
        machine_id = machine.id
        first_id, second_id = first.id, second.id
        start, end = first.scheduled_start, first.scheduled_start + timedelta(minutes=90)

    ready, release = threading.Event(), threading.Event()
    result = {}

    def assign_first():
        with committed_session_factory() as session:
            farm_row = session.get(Farm, farm_id)
            repo = _PrecheckedRepo(
                session,
                farm_id,
                ensure_owner_grant(session, farm_row),
                "race",
                "test",
                ready=ready,
                release=release,
            )
            job = service.locked(repo, first_id)
            try:
                service.set_assignments(
                    repo,
                    NOW,
                    "Europe/Paris",
                    job,
                    expected_revision=1,
                    changes={"machine": machine_id},
                )
            except Exception as exc:  # the point of the test is which error this is
                result["error"] = exc
            else:
                result["ok"] = True

    thread = threading.Thread(target=assign_first)
    thread.start()
    assert ready.wait(timeout=10)
    with committed_session_factory.begin() as session:
        session.add(
            PlanningJobAssignment(
                farm_id=farm_id,
                job_id=second_id,
                machine_id=machine_id,
                starts_at=start,
                ends_at=end,
                created_by="race",
                updated_by="race",
            )
        )
    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), service.ResourceConflict)
    assert "already booked" in str(result["error"])
    with committed_session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(PlanningJobAssignment)
                .where(PlanningJobAssignment.job_id == first_id)
            )
            == 0
        )
