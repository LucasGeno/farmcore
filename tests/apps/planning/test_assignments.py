"""A planned job books one machine and one operator; overlapping bookings are refused."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobAssignment,
    PlanningJobRevision,
    PlanningMachine,
    PlanningOperator,
)
from farmcore.core.access import ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm
from farmcore.main import create_app
from tests.apps.planning.conftest import NOW, job_payload
from tests.apps.share.conftest import make_parcel


def _machine(session, farm, name="Sprayer 1", active=True):
    row = PlanningMachine(
        farm_id=farm.id,
        name=name,
        kind="sprayer",
        work_rate_ha_per_hour=None,
        active=active,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def _operator(session, farm, name="Ana", active=True):
    row = PlanningOperator(
        farm_id=farm.id,
        name=name,
        active=active,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def _created(client, farm, parcels, products, advice, **overrides):
    response = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _assign(client, farm, job_id, kind, resource_id, revision):
    return client.post(
        f"/planning/jobs/{job_id}/assignments",
        params={"farm": farm.id},
        json={"expected_revision": revision, "kind": kind, "resource_id": resource_id},
    )


def test_owner_assigns_machine_and_operator_and_keeps_evidence(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    operator = _operator(session, farm)
    created = _created(client, farm, parcels, products, advice)

    first = _assign(client, farm, created["id"], "machine", machine.id, 1)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["revision"] == 2
    assert [(a["kind"], a["name"]) for a in body["assignments"]] == [("machine", "Sprayer 1")]
    assert body["assignments"][0]["starts_at"].startswith("2024-04-09T06:00:00")

    second = _assign(client, farm, created["id"], "operator", operator.id, 2)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["revision"] == 3
    assert [(a["kind"], a["name"]) for a in body["assignments"]] == [
        ("machine", "Sprayer 1"),
        ("operator", "Ana"),
    ]

    session.expire_all()
    revision = session.scalar(
        select(PlanningJobRevision)
        .where(PlanningJobRevision.job_id == created["id"])
        .order_by(PlanningJobRevision.revision.desc())
        .limit(1)
    )
    assert [(a["kind"], a["name"]) for a in revision.snapshot["assignments"]] == [
        ("machine", "Sprayer 1"),
        ("operator", "Ana"),
    ]

    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert "Sprayer 1" in page.text and "Ana" in page.text


def test_overlapping_assignments_are_rejected_and_write_nothing(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    first = _created(client, farm, parcels, products, advice)
    second = _created(client, farm, parcels, products, advice)

    assert _assign(client, farm, first["id"], "machine", machine.id, 1).status_code == 200
    clash = _assign(client, farm, second["id"], "machine", machine.id, 1)
    assert clash.status_code == 409
    assert "already booked" in clash.text

    session.expire_all()
    assert session.get(PlanningJob, second["id"]).revision == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobAssignment)
            .where(PlanningJobAssignment.job_id == second["id"])
        )
        == 0
    )

    # Half-open: the second job starts exactly when the first ends and is accepted.
    back_to_back = (NOW + timedelta(hours=3, minutes=30)).isoformat()
    moved = client.patch(
        f"/planning/jobs/{second['id']}",
        params={"farm": farm.id},
        json={"expected_revision": 1, "scheduled_start": back_to_back},
    )
    assert moved.status_code == 200, moved.text
    assert _assign(client, farm, second["id"], "machine", machine.id, 2).status_code == 200


def test_assignments_reject_foreign_unconfigured_and_inactive_resources(
    client, session, farm, parcels, products, advice
):
    other = Farm(name="Other assignment farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    foreign = _machine(session, other, name="Foreign sprayer")
    inactive = _machine(session, farm, name="Retired sprayer", active=False)
    created = _created(client, farm, parcels, products, advice)

    for resource_id in (foreign.id, inactive.id, 999999):
        response = _assign(client, farm, created["id"], "machine", resource_id, 1)
        assert response.status_code == 422
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobAssignment)
            .where(PlanningJobAssignment.job_id == created["id"])
        )
        == 0
    )


def test_cancelling_a_job_frees_resources_and_keeps_snapshot_evidence(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    first = _created(client, farm, parcels, products, advice)
    second = _created(client, farm, parcels, products, advice)
    assert _assign(client, farm, first["id"], "machine", machine.id, 1).status_code == 200
    cancelled = client.post(
        f"/planning/jobs/{first['id']}/cancel",
        params={"farm": farm.id},
        json={"expected_revision": 2, "reason": "Weather"},
    )
    assert cancelled.status_code == 200, cancelled.text
    session.expire_all()
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobAssignment)
            .where(PlanningJobAssignment.job_id == first["id"])
        )
        == 0
    )
    revision = session.scalar(
        select(PlanningJobRevision)
        .where(PlanningJobRevision.job_id == first["id"])
        .order_by(PlanningJobRevision.revision.desc())
        .limit(1)
    )
    assert revision.snapshot["assignments"][0]["name"] == "Sprayer 1"
    assert _assign(client, farm, second["id"], "machine", machine.id, 1).status_code == 200


def test_editing_a_job_moves_its_assignments_or_refuses_a_conflict(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    first = _created(client, farm, parcels, products, advice)
    second = _created(
        client,
        farm,
        parcels,
        products,
        advice,
        scheduled_start=(NOW + timedelta(hours=4)).isoformat(),
    )
    assert _assign(client, farm, first["id"], "machine", machine.id, 1).status_code == 200
    assert _assign(client, farm, second["id"], "machine", machine.id, 1).status_code == 200

    clash = client.patch(
        f"/planning/jobs/{first['id']}",
        params={"farm": farm.id},
        json={
            "expected_revision": 2,
            "scheduled_start": (NOW + timedelta(hours=4)).isoformat(),
        },
    )
    assert clash.status_code == 409
    session.expire_all()
    assert session.get(PlanningJob, first["id"]).revision == 2
    booking = session.scalar(
        select(PlanningJobAssignment).where(PlanningJobAssignment.job_id == first["id"])
    )
    assert booking.starts_at == NOW + timedelta(hours=2)

    moved = client.patch(
        f"/planning/jobs/{first['id']}",
        params={"farm": farm.id},
        json={
            "expected_revision": 2,
            "scheduled_start": (NOW + timedelta(hours=8)).isoformat(),
        },
    )
    assert moved.status_code == 200, moved.text
    session.expire_all()
    booking = session.scalar(
        select(PlanningJobAssignment).where(PlanningJobAssignment.job_id == first["id"])
    )
    assert booking.starts_at == NOW + timedelta(hours=8)
    assert booking.ends_at == NOW + timedelta(hours=9, minutes=30)


def test_the_assignment_form_sets_and_removes_resources_the_template_sends(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    operator = _operator(session, farm)
    created = _created(client, farm, parcels, products, advice)

    response = client.post(
        f"/planning/jobs/{created['id']}/assign-form",
        params={"farm": farm.id},
        data={
            "expected_revision": "1",
            "machine_id": str(machine.id),
            "operator_id": str(operator.id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    job = session.get(PlanningJob, created["id"])
    assert job.revision == 2
    rows = session.scalars(
        select(PlanningJobAssignment).where(PlanningJobAssignment.job_id == job.id)
    ).all()
    assert {(row.machine_id, row.operator_id) for row in rows} == {
        (machine.id, None),
        (None, operator.id),
    }

    removed = client.post(
        f"/planning/jobs/{created['id']}/assign-form",
        params={"farm": farm.id},
        data={"expected_revision": "2", "machine_id": "", "operator_id": ""},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    session.expire_all()
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobAssignment)
            .where(PlanningJobAssignment.job_id == created["id"])
        )
        == 0
    )


def test_the_assignment_form_reports_a_conflict_instead_of_failing(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    first = _created(client, farm, parcels, products, advice)
    second = _created(client, farm, parcels, products, advice)
    assert _assign(client, farm, first["id"], "machine", machine.id, 1).status_code == 200

    response = client.post(
        f"/planning/jobs/{second['id']}/assign-form",
        params={"farm": farm.id},
        data={
            "expected_revision": "1",
            "machine_id": str(machine.id),
            "operator_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    session.expire_all()
    assert session.get(PlanningJob, second["id"]).revision == 1


def test_demo_reset_clears_assignments_and_resources(
    client, session, farm, parcels, products, advice
):
    from farmcore.core import demo_control, registry

    machine = _machine(session, farm)
    created = _created(client, farm, parcels, products, advice)
    assert _assign(client, farm, created["id"], "machine", machine.id, 1).status_code == 200
    session.expire_all()

    demo_control._clear(session, farm, registry.discover())
    session.flush()
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(PlanningJobAssignment)) == 0
    assert session.scalar(select(func.count()).select_from(PlanningMachine)) == 0


def test_concurrent_assignments_to_one_machine_have_one_winner(settings, committed_session_factory):
    with committed_session_factory.begin() as session:
        farm = Farm(name="Concurrent resources farm", props={"region": "FRI12"})
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
        machine = PlanningMachine(
            farm_id=farm.id,
            name="Shared sprayer",
            kind="sprayer",
            active=True,
            notes="",
            created_by="owner",
            updated_by="owner",
        )
        session.add(machine)
        session.flush()
        set_replay(session, NOW, settings)
        farm_id = farm.id
        machine_id = machine.id
        payload = job_payload([parcel], products, advice)

    app = create_app(settings, session_factory=committed_session_factory)
    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    with TestClient(app, headers=headers) as browser:
        jobs = [
            browser.post("/planning/jobs", params={"farm": farm_id}, json=payload).json()["id"]
            for _ in range(2)
        ]

    def assign(job_id):
        with TestClient(app, headers=headers) as browser:
            return browser.post(
                f"/planning/jobs/{job_id}/assignments",
                params={"farm": farm_id},
                json={"expected_revision": 1, "kind": "machine", "resource_id": machine_id},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(assign, jobs))

    assert sorted(r.status_code for r in responses) == [200, 409]
    with committed_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanningJobAssignment)) == 1
        assert session.scalar(select(func.count()).select_from(PlanningJobRevision)) == 3


def test_a_concurrent_booking_that_beats_the_precheck_is_a_clean_job_conflict(
    settings, committed_session_factory
):
    """One session holds a new booking open while the edit's pre-check cannot see it.

    The edit's flush then reaches the database exclusion constraint (23P01); the service must
    translate that into the same 409-style conflict as the assignment endpoint, not a 500.
    """
    import threading

    from sqlalchemy import text

    from farmcore.apps.planning import service as planning_service
    from farmcore.core.access import ScopedRepository

    with committed_session_factory.begin() as session:
        farm = Farm(name="Concurrent edit resource farm", props={"region": "FRI12"})
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
        machine = PlanningMachine(
            farm_id=farm.id,
            name="Shared sprayer",
            kind="sprayer",
            active=True,
            notes="",
            created_by="owner",
            updated_by="owner",
        )
        session.add(machine)
        session.flush()
        jobs = []
        for offset in (2, 4):
            job = PlanningJob(
                farm_id=farm.id,
                status="planned",
                revision=1,
                name=f"Job {offset}",
                assessment_id=advice.id,
                config_version_status="unversioned",
                product_id=products["copper-contact"].id,
                scheduled_start=NOW + timedelta(hours=offset),
                duration_minutes=90,
                notes="",
                created_by="owner",
                updated_by="owner",
            )
            session.add(job)
            session.flush()
            session.add(
                PlanningJobAssignment(
                    farm_id=farm.id,
                    job_id=job.id,
                    machine_id=machine.id,
                    starts_at=job.scheduled_start,
                    ends_at=job.scheduled_start + timedelta(minutes=90),
                    created_by="owner",
                    updated_by="owner",
                )
            )
            jobs.append(job)
        set_replay(session, NOW, settings)
        farm_id = farm.id
        first_id, second_id = (job.id for job in jobs)
        machine_id = machine.id

    target = NOW + timedelta(hours=8)
    ready = threading.Event()

    def hold_new_booking():
        with committed_session_factory() as holder:
            holder.execute(
                text(
                    "UPDATE planning_job_assignment SET starts_at=:start, ends_at=:end "
                    "WHERE job_id=:job"
                ),
                {"start": target, "end": target + timedelta(minutes=90), "job": first_id},
            )
            ready.set()
            import time

            time.sleep(1.5)
            holder.commit()

    thread = threading.Thread(target=hold_new_booking)
    thread.start()
    assert ready.wait(5)

    with committed_session_factory() as session:
        repo = ScopedRepository(
            session,
            farm_id,
            ensure_owner_grant(session, session.get(Farm, farm_id)),
            "test",
            "test",
        )
        job = planning_service.locked(repo, second_id)
        with pytest.raises(planning_service.JobConflict) as raised:
            planning_service.edit(
                repo,
                NOW,
                "Europe/Paris",
                job,
                expected_revision=1,
                changes={"scheduled_start": target},
            )
    thread.join(timeout=10)
    assert "another job at the new time" in str(raised.value)

    with committed_session_factory() as session:
        assert session.get(PlanningJob, second_id).scheduled_start == NOW + timedelta(hours=4)
        booking = session.scalar(
            select(PlanningJobAssignment).where(PlanningJobAssignment.job_id == second_id)
        )
        assert booking.machine_id == machine_id
        assert booking.starts_at == NOW + timedelta(hours=4)
