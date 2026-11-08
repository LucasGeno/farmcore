"""Coverage for planning validation, form and pure-scheduling branches through real seams."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs
from farmcore.apps.forecast import sources, store
from farmcore.apps.planning import engine, offers, resources, service
from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobParcel,
    PlanningJobRevision,
    PlanningMachine,
    PlanningOperator,
    PlanningResourceWindow,
)
from farmcore.core.access import DataGrant, ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Advice
from tests.apps.planning.conftest import NOW, job_payload

FIXTURE = Path(__file__).parents[1] / "forecast" / "fixtures" / "forecast-runs.json"


def _repo(session, farm):
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")


def _advice(session, farm, parcel, **overrides):
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=overrides.pop("evaluated_at", NOW),
        decision=overrides.pop("decision", "spray"),
        reasons=[],
        products=[],
        props={"assessment_status": "evaluable", "config_version": {"id": None, "status": "x"}},
        **overrides,
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


def _job(session, farm, parcel, product):
    advice = _advice(session, farm, parcel)
    return service.create(
        _repo(session, farm),
        NOW,
        "Europe/Paris",
        name="Coverage job",
        parcel_ids=[parcel.id],
        assessment_id=advice.id,
        product_id=product.id,
        scheduled_start=NOW + timedelta(hours=2),
        duration_minutes=90,
    )


def _completion(parcel, product, **overrides):
    values = {
        "expected_revision": 1,
        "request_key": "coverage-key",
        "actual_start": NOW - timedelta(hours=2),
        "duration_minutes": 60,
        "parcel_ids": [parcel.id],
        "quantities": [{"product_id": product.id, "value": "1.5", "unit": "kg/ha"}],
        "notes": "",
    }
    values.update(overrides)
    return values


def _form(job_id, expected_revision=1, **overrides):
    values = {
        "expected_revision": str(expected_revision),
        "request_key": "coverage-form-key",
        "actual_start": "2024-04-09T04:00",
        "duration_minutes": "60",
        "parcel_ids": [],
        "product_id": [],
        "dose_value": [],
        "dose_unit": [],
        "notes": "",
    }
    values.update(overrides)
    return values


def test_json_list_and_new_prefill_variants(client, session, farm, parcels, products, advice):
    assert client.get("/planning/jobs.json", params={"farm": farm.id}).status_code == 200
    for params in (
        {},
        {"parcel": parcels[0].id},
        {"assessment": advice.id},
        {"day": "2024-04-10"},
        {"parcel": 999999},
    ):
        response = client.get("/planning/new", params={"farm": farm.id, **params})
        assert response.status_code == 200, response.text


def test_create_json_rejects_duplicate_parcels(client, farm, parcels, products, advice):
    payload = job_payload(parcels, products, advice)
    payload["parcel_ids"] = [parcels[0].id, parcels[0].id]
    response = client.post("/planning/jobs", params={"farm": farm.id}, json=payload)
    assert response.status_code == 422


def test_form_routes_report_clean_redirects(client, session, farm, parcels, products, advice):
    # Creation validation.
    response = client.post(
        "/planning/jobs-form", params={"farm": farm.id}, data={"name": ""}, follow_redirects=False
    )
    assert response.status_code == 303 and "error=" in response.headers["location"]

    created = _created(client, farm, parcels, products, advice)
    job_id = created["id"]

    # Unknown job ids and invalid/stale edits.
    assert (
        client.post(
            "/planning/jobs/999999/edit-form",
            params={"farm": farm.id},
            data={
                "expected_revision": "1",
                "name": "Missing",
                "parcel_ids": [str(parcels[0].id)],
                "product_id": str(products["copper-contact"].id),
                "scheduled_start": "2024-04-09T08:00",
                "duration_minutes": "90",
                "notes": "",
            },
        ).status_code
        == 404
    )
    stale = client.post(
        f"/planning/jobs/{job_id}/edit-form",
        params={"farm": farm.id},
        data={
            "expected_revision": "99",
            "name": "Stale",
            "parcel_ids": [str(parcels[0].id)],
            "product_id": str(products["copper-contact"].id),
            "scheduled_start": "2024-04-09T08:00",
            "duration_minutes": "90",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert stale.status_code == 303 and "error=" in stale.headers["location"]
    invalid = client.post(
        f"/planning/jobs/{job_id}/edit-form",
        params={"farm": farm.id},
        data={"expected_revision": "1", "duration_minutes": "not-a-number"},
        follow_redirects=False,
    )
    assert invalid.status_code == 303 and "error=" in invalid.headers["location"]
    edited = client.post(
        f"/planning/jobs/{job_id}/edit-form",
        params={"farm": farm.id},
        data={
            "expected_revision": "1",
            "name": "Edited by form",
            "parcel_ids": [str(parcels[0].id)],
            "product_id": str(products["copper-contact"].id),
            "scheduled_start": "2024-04-09T08:00",
            "duration_minutes": "90",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303 and "error=" not in edited.headers["location"]
    session.expire_all()
    assert session.get(PlanningJob, job_id).revision == 2

    # Assignment and cancellation error branches.
    assert (
        client.post(
            "/planning/jobs/999999/assign-form", params={"farm": farm.id}, data={}
        ).status_code
        == 404
    )
    bad_assignment = client.post(
        f"/planning/jobs/{job_id}/assign-form",
        params={"farm": farm.id},
        data={"expected_revision": "2", "machine_id": "not-a-number"},
        follow_redirects=False,
    )
    assert bad_assignment.status_code == 303 and "error=" in bad_assignment.headers["location"]
    empty_assignment = client.post(
        f"/planning/jobs/{job_id}/assign-form",
        params={"farm": farm.id},
        data={"expected_revision": "2"},
        follow_redirects=False,
    )
    assert empty_assignment.status_code == 303 and "error=" in empty_assignment.headers["location"]
    assert (
        client.post(
            "/planning/jobs/999999/cancel-form",
            params={"farm": farm.id},
            data={"expected_revision": "1"},
        ).status_code
        == 404
    )
    bad_cancel = client.post(
        f"/planning/jobs/{job_id}/cancel-form",
        params={"farm": farm.id},
        data={"expected_revision": "not-a-number"},
        follow_redirects=False,
    )
    assert bad_cancel.status_code == 303 and "error=" in bad_cancel.headers["location"]
    stale_cancel = client.post(
        f"/planning/jobs/{job_id}/cancel-form",
        params={"farm": farm.id},
        data={"expected_revision": "1", "reason": "stale"},
        follow_redirects=False,
    )
    assert stale_cancel.status_code == 303 and "error=" in stale_cancel.headers["location"]
    cancelled = client.post(
        f"/planning/jobs/{job_id}/cancel-form",
        params={"farm": farm.id},
        data={"expected_revision": "2", "reason": "form cancel"},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303 and "error=" not in cancelled.headers["location"]

    # Completion form: unknown, stale, malformed and blank-row branches.
    assert (
        client.post(
            "/planning/jobs/999999/complete-preview-form",
            params={"farm": farm.id},
            data=_form(
                999999,
                parcel_ids=[str(parcels[0].id)],
                product_id=[str(products["copper-contact"].id)],
                dose_value=["1.5"],
                dose_unit=["kg/ha"],
            ),
        ).status_code
        == 404
    )
    fresh = _created(client, farm, parcels, products, advice, name="Second")
    form = _form(
        fresh["id"],
        parcel_ids=[str(parcels[0].id)],
        product_id=[str(products["copper-contact"].id)],
        dose_value=["1.5"],
        dose_unit=["kg/ha"],
    )
    stale_preview = client.post(
        f"/planning/jobs/{fresh['id']}/complete-preview-form",
        params={"farm": farm.id},
        data={**form, "expected_revision": "99"},
        follow_redirects=False,
    )
    assert stale_preview.status_code == 303 and "error=" in stale_preview.headers["location"]
    mismatched = client.post(
        f"/planning/jobs/{fresh['id']}/complete-preview-form",
        params={"farm": farm.id},
        data={**form, "dose_unit": []},
        follow_redirects=False,
    )
    assert mismatched.status_code == 303 and "error=" in mismatched.headers["location"]
    half_row = client.post(
        f"/planning/jobs/{fresh['id']}/complete-preview-form",
        params={"farm": farm.id},
        data={**form, "dose_value": [""]},
        follow_redirects=False,
    )
    assert half_row.status_code == 303 and "error=" in half_row.headers["location"]
    blank_row = client.post(
        f"/planning/jobs/{fresh['id']}/complete-preview-form",
        params={"farm": farm.id},
        data={
            **form,
            "product_id": ["", str(products["copper-contact"].id)],
            "dose_value": ["", "1.5"],
            "dose_unit": ["kg/ha", "kg/ha"],
        },
        follow_redirects=False,
    )
    assert blank_row.status_code == 200
    assert "Confirm completed work" in blank_row.text
    assert (
        client.post(
            "/planning/jobs/999999/complete-form",
            params={"farm": farm.id},
            data=form,
        ).status_code
        == 404
    )
    stale_complete = client.post(
        f"/planning/jobs/{fresh['id']}/complete-form",
        params={"farm": farm.id},
        data={**form, "expected_revision": "99"},
        follow_redirects=False,
    )
    assert stale_complete.status_code == 303 and "error=" in stale_complete.headers["location"]

    duplicate = job_payload(parcels, products, advice)
    duplicate["parcel_ids"] = [parcels[0].id, parcels[0].id]
    assert (
        client.patch(
            f"/planning/jobs/{fresh['id']}",
            params={"farm": farm.id},
            json={"expected_revision": 1, "parcel_ids": duplicate["parcel_ids"]},
        ).status_code
        == 422
    )
    completion = _completion(parcels[0], products["copper-contact"], request_key=" ")
    assert (
        client.post(
            f"/planning/jobs/{fresh['id']}/complete",
            params={"farm": farm.id},
            json={
                **completion,
                "actual_start": completion["actual_start"].isoformat(),
            },
        ).status_code
        == 422
    )
    duplicate_completion = _completion(
        parcels[0],
        products["copper-contact"],
        request_key="duplicate-parcels",
        parcel_ids=[parcels[0].id, parcels[0].id],
    )
    assert (
        client.post(
            f"/planning/jobs/{fresh['id']}/complete",
            params={"farm": farm.id},
            json={
                **duplicate_completion,
                "actual_start": duplicate_completion["actual_start"].isoformat(),
            },
        ).status_code
        == 422
    )


def test_service_create_validation_branches(session, farm, parcels, products, advice):
    repo = _repo(session, farm)
    with pytest.raises(service.JobError, match="too long"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x" * 201,
            parcel_ids=[parcels[0].id],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW,
            duration_minutes=60,
        )
    with pytest.raises(service.JobError, match="at least one parcel"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x",
            parcel_ids=[],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW,
            duration_minutes=60,
        )
    none = _advice(session, farm, parcels[0], decision="none")
    with pytest.raises(service.JobError, match="Only spray or wait"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x",
            parcel_ids=[parcels[0].id],
            assessment_id=none.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW,
            duration_minutes=60,
        )
    future = _advice(session, farm, parcels[0], evaluated_at=NOW + timedelta(hours=1))
    with pytest.raises(service.JobError, match="from the future"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x",
            parcel_ids=[parcels[0].id],
            assessment_id=future.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW,
            duration_minutes=60,
        )
    with pytest.raises(service.JobError, match="shortlist"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x",
            parcel_ids=[parcels[0].id],
            assessment_id=advice.id,
            product_id=0,
            scheduled_start=NOW,
            duration_minutes=60,
        )
    with pytest.raises(service.JobError, match="timezone"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x",
            parcel_ids=[parcels[0].id],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=datetime(2024, 4, 9, 8),
            duration_minutes=60,
        )
    with pytest.raises(service.JobError, match="between 1 and 1440"):
        service.create(
            repo,
            NOW,
            "Europe/Paris",
            name="x",
            parcel_ids=[parcels[0].id],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=NOW,
            duration_minutes=0,
        )


def test_service_locked_and_job_not_found(session, farm):
    repo = _repo(session, farm)
    for lookup in (service.locked, service.job):
        with pytest.raises(service.JobNotFound):
            lookup(repo, 0)
        with pytest.raises(service.JobNotFound):
            lookup(repo, 999999)


def test_service_edit_branches(session, farm, parcels, products):
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    with pytest.raises(service.JobError, match="No changes"):
        service.edit(repo, NOW, "Europe/Paris", job, expected_revision=1, changes={})
    with pytest.raises(service.JobError, match="timezone"):
        service.edit(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=1,
            changes={"scheduled_start": "not-a-time"},
        )
    edited = service.edit(
        repo,
        NOW,
        "Europe/Paris",
        job,
        expected_revision=1,
        changes={"parcel_ids": [parcels[0].id, parcels[1].id]},
    )
    assert edited.revision == 2
    edited = service.edit(
        repo,
        NOW,
        "Europe/Paris",
        job,
        expected_revision=2,
        changes={"product_id": products["sulphur-wettable"].id},
    )
    assert edited.product_id == products["sulphur-wettable"].id
    with pytest.raises(service.JobError, match="timezone"):
        service.edit(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=3,
            changes={"scheduled_start": datetime(2024, 4, 9, 8)},
        )
    with pytest.raises(service.JobError, match="between 1 and 1440"):
        service.edit(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=3,
            changes={"duration_minutes": 0},
        )
    with pytest.raises(service.JobError, match="must be one of the job's parcels"):
        service.edit(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=3,
            changes={"parcel_ids": [parcels[1].id]},
        )
    with pytest.raises(IntegrityError):
        service.edit(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=3,
            changes={"scheduled_start": NOW + timedelta(hours=3), "notes": "x" * 10001},
        )
    session.rollback()


def test_service_cancel_and_assignment_branches(session, farm, parcels, products):
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    with pytest.raises(service.JobError, match="No resource changes"):
        service.set_assignments(
            repo, NOW, "Europe/Paris", job, expected_revision=1, changes={"other": 1}
        )
    with pytest.raises(service.JobConflict, match="Revision conflict"):
        service.set_assignments(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=99,
            changes={"machine": None},
        )
    machine = resources.create_machine(
        repo, NOW, name="Coverage sprayer", kind="sprayer", work_rate_ha_per_hour="2"
    )
    job = service.set_assignments(
        repo, NOW, "Europe/Paris", job, expected_revision=1, changes={"machine": machine.id}
    )
    assert job.revision == 2
    job = service.set_assignments(
        repo, NOW, "Europe/Paris", job, expected_revision=2, changes={"machine": machine.id}
    )
    assert job.revision == 3
    job = service.cancel(repo, NOW, "Europe/Paris", job, expected_revision=3, reason="done")
    with pytest.raises(service.JobConflict, match="already cancelled"):
        service.cancel(repo, NOW, "Europe/Paris", job, expected_revision=4)
    with pytest.raises(service.JobConflict, match="cannot be rebooked"):
        service.set_assignments(
            repo, NOW, "Europe/Paris", job, expected_revision=4, changes={"machine": None}
        )


def test_service_completion_validation_branches(session, farm, parcels, products):
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    valid = _completion(parcels[0], products["copper-contact"])
    for override, message in (
        ({"quantities": []}, "at least one applied product"),
        ({"quantities": [{"value": "1", "unit": "kg/ha"}]}, "needs a product"),
        ({"quantities": [{"product_id": 0, "value": "1", "unit": "kg/ha"}]}, "shortlist"),
        (
            {
                "quantities": [
                    {"product_id": products["copper-contact"].id, "value": "0", "unit": "kg/ha"}
                ]
            },
            "positive",
        ),
        (
            {
                "quantities": [
                    {"product_id": products["copper-contact"].id, "value": "1", "unit": "cups"}
                ]
            },
            "rate unit",
        ),
        (
            {
                "quantities": [
                    {"product_id": products["copper-contact"].id, "value": "1", "unit": "kg/ha"},
                    {"product_id": products["copper-contact"].id, "value": "2", "unit": "kg/ha"},
                ]
            },
            "each product once",
        ),
        ({"actual_start": datetime(2024, 4, 9, 1)}, "timezone"),
        ({"duration_minutes": 0}, "between 1 and 1440"),
        ({"notes": "x" * 10001}, "too long"),
    ):
        with pytest.raises(service.JobError, match=message):
            service.complete(repo, NOW, "Europe/Paris", job.id, **{**valid, **override})
    with pytest.raises(service.JobConflict, match="Revision conflict"):
        service.complete(repo, NOW, "Europe/Paris", job.id, **{**valid, "expected_revision": 99})
    service.complete(repo, NOW, "Europe/Paris", job.id, **valid)
    with pytest.raises(service.JobConflict, match="already completed"):
        service.completion_preview(
            repo, service.job(repo, job.id), now=NOW, tz="Europe/Paris", **valid
        )
    other = _job(session, farm, parcels[1], products["copper-contact"])
    service.cancel(repo, NOW, "Europe/Paris", other, expected_revision=1)
    with pytest.raises(service.JobConflict, match="Only planned jobs"):
        service.complete(
            repo,
            NOW,
            "Europe/Paris",
            other.id,
            **{**valid, "request_key": "cancelled-key"},
        )


def test_completion_preview_reports_removed_parcels_and_products(session, farm, parcels, products):
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    preview = service.completion_preview(
        repo,
        job,
        now=NOW,
        tz="Europe/Paris",
        expected_revision=1,
        request_key="preview-diff",
        actual_start=NOW - timedelta(hours=2),
        duration_minutes=60,
        parcel_ids=[parcels[1].id],
        quantities=[{"product_id": products["sulphur-wettable"].id, "value": "1", "unit": "kg/ha"}],
        notes="",
    )
    differences = " ".join(preview["differences"])
    assert "not treated" in differences and "Products:" in differences


def test_service_completion_constraint_failures_roll_back(session, farm, parcels, products):
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    session.add(
        PlanningJobRevision(
            farm_id=farm.id,
            job_id=job.id,
            revision=2,
            action="edited",
            status="planned",
            snapshot={},
            actor="test",
            reason="",
            created_at=NOW,
        )
    )
    session.flush()
    with pytest.raises(IntegrityError):
        service.complete(
            repo,
            NOW,
            "Europe/Paris",
            job.id,
            **_completion(parcels[0], products["copper-contact"]),
        )
    session.rollback()


def test_job_json_tolerates_a_job_without_parcel_links(session, farm, parcels, products):
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    session.execute(delete(PlanningJobParcel).where(PlanningJobParcel.job_id == job.id))
    session.flush()
    assert service.job_json(repo, job, now=NOW, tz="Europe/Paris")["parcels"] == []


def test_resource_service_validation(session, farm):
    repo = _repo(session, farm)
    with pytest.raises(resources.ResourceError, match="too long"):
        resources.create_machine(repo, NOW, name="x" * 121, kind="sprayer")
    with pytest.raises(resources.ResourceError, match="Notes are too long"):
        resources.create_machine(repo, NOW, name="x", kind="sprayer", notes="x" * 2001)
    with pytest.raises(resources.ResourceError, match="must be a number"):
        resources.create_machine(repo, NOW, name="x", kind="sprayer", work_rate_ha_per_hour="no")
    with pytest.raises(resources.ResourceError, match="above the value"):
        resources.create_machine(
            repo, NOW, name="x", kind="sprayer", work_rate_ha_per_hour="1000000"
        )
    machine = resources.create_machine(repo, NOW, name="Duplicate", kind="sprayer")
    with pytest.raises(resources.ResourceError, match="already exists"):
        resources.create_machine(repo, NOW, name="Duplicate", kind="sprayer")
    with pytest.raises(resources.ResourceError, match="already exists"):
        resources.create_operator(repo, NOW, name="Ana")
        resources.create_operator(repo, NOW, name="Ana")
    with pytest.raises(resources.ResourceError, match="Choose a machine or an operator"):
        resources.create_window(
            repo,
            NOW,
            kind="other",
            resource_id=1,
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            available=True,
        )
    with pytest.raises(resources.ResourceError, match="timezone"):
        resources.create_window(
            repo,
            NOW,
            kind="machine",
            resource_id=machine.id,
            starts_at=datetime(2024, 4, 9, 5),
            ends_at=NOW,
            available=True,
        )
    with pytest.raises(resources.ResourceError, match="end after it starts"):
        resources.create_window(
            repo,
            NOW,
            kind="machine",
            resource_id=machine.id,
            starts_at=NOW,
            ends_at=NOW,
            available=True,
        )
    resources.set_active(repo, NOW, kind="machine", resource_id=machine.id, active=False)
    with pytest.raises(resources.ResourceError, match="inactive"):
        resources.create_window(
            repo,
            NOW,
            kind="machine",
            resource_id=machine.id,
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            available=True,
        )
    with pytest.raises(resources.ResourceError, match="Choose a machine or an operator"):
        resources.set_active(repo, NOW, kind="other", resource_id=1, active=True)


def test_resource_database_duplicate_fallbacks(session, farm):
    repo = _repo(session, farm)
    resources.create_machine(repo, NOW, name="Existing", kind="sprayer")
    with session.no_autoflush:
        session.add(
            resources.PlanningMachine(
                farm_id=farm.id,
                name="Pending duplicate",
                kind="sprayer",
                active=True,
                notes="",
                created_by="t",
                updated_by="t",
            )
        )
        with pytest.raises(resources.ResourceError, match="already exists"):
            resources.create_machine(repo, NOW, name="Pending duplicate", kind="sprayer")
    session.rollback()


def test_operator_database_duplicate_fallback(session, farm):
    repo = _repo(session, farm)
    resources.create_operator(repo, NOW, name="Existing operator")
    with session.no_autoflush:
        session.add(
            resources.PlanningOperator(
                farm_id=farm.id,
                name="Pending operator",
                active=True,
                notes="",
                created_by="t",
                updated_by="t",
            )
        )
        with pytest.raises(resources.ResourceError, match="already exists"):
            resources.create_operator(repo, NOW, name="Pending operator")
    session.rollback()


def test_resource_form_error_branches(client, session, farm):
    bad_available = client.post(
        "/planning/resources/windows-form",
        params={"farm": farm.id},
        data={
            "resource": "machine:1",
            "starts_at": "2024-04-09T05:00",
            "ends_at": "2024-04-09T06:00",
            "available": "maybe",
        },
        follow_redirects=False,
    )
    assert bad_available.status_code == 303 and "error=" in bad_available.headers["location"]
    long_operator = client.post(
        "/planning/resources/operators-form",
        params={"farm": farm.id},
        data={"name": "x" * 121},
        follow_redirects=False,
    )
    assert long_operator.status_code == 303 and "error=" in long_operator.headers["location"]
    bad_resource = client.post(
        "/planning/resources/windows-form",
        params={"farm": farm.id},
        data={
            "resource": "nonsense",
            "starts_at": "2024-04-09T05:00",
            "ends_at": "2024-04-09T06:00",
            "available": "1",
        },
        follow_redirects=False,
    )
    assert bad_resource.status_code == 303 and "error=" in bad_resource.headers["location"]
    bad_window = client.post(
        "/planning/resources/windows-form",
        params={"farm": farm.id},
        data={
            "resource": "machine:1",
            "starts_at": "2024-04-09T05:00",
            "ends_at": "2024-04-09T05:00",
            "available": "1",
        },
        follow_redirects=False,
    )
    assert bad_window.status_code == 303 and "error=" in bad_window.headers["location"]
    bad_kind = client.post(
        "/planning/resources/other/1/active-form",
        params={"farm": farm.id},
        data={"active": "1"},
        follow_redirects=False,
    )
    assert bad_kind.status_code == 303 and "error=" in bad_kind.headers["location"]


def _forecast(session, farm):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    session.flush()


def _machine(session, farm, name, *, work_rate=None):
    row = PlanningMachine(
        farm_id=farm.id,
        name=name,
        kind="sprayer",
        work_rate_ha_per_hour=work_rate,
        active=True,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def _operator(session, farm, name):
    row = PlanningOperator(
        farm_id=farm.id, name=name, active=True, notes="", created_by="owner", updated_by="owner"
    )
    session.add(row)
    session.flush()
    return row


def _window(session, farm, *, machine=None, operator=None):
    row = PlanningResourceWindow(
        farm_id=farm.id,
        machine_id=machine.id if machine else None,
        operator_id=operator.id if operator else None,
        starts_at=NOW.replace(hour=0, minute=0),
        ends_at=NOW.replace(hour=23, minute=0),
        available=True,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def test_offers_scope_denied_paths(session, farm, parcels, products):
    job = _job(session, farm, parcels[0], products["copper-contact"])
    narrow = DataGrant(farm_id=farm.id, grantee="narrow", scopes=["planning:read"], purpose="test")
    session.add(narrow)
    session.flush()
    repo = ScopedRepository(session, farm.id, narrow, "test", "test")
    forecast = offers.forecast_context(repo, NOW, "Europe/Paris")
    assert forecast.run is None
    assert "not authorized" in forecast.reason
    assert offers._busy_days(repo, NOW, "Europe/Paris") == set()
    span = engine.Span(job.scheduled_start, job.scheduled_start + timedelta(minutes=90))
    assert offers._parcel_offers(repo, job, NOW, span) == []

    no_planning = DataGrant(farm_id=farm.id, grantee="fields", scopes=["fields:read"], purpose="x")
    session.add(no_planning)
    session.flush()
    strict = ScopedRepository(session, farm.id, no_planning, "test", "test")
    assert offers._job_offers(strict, job, NOW, "Europe/Paris", forecast, span, set()) == []


def test_machine_offers_unknown_states_and_short_capacity(session, farm, parcels, products):
    _forecast(session, farm)
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    unknown = _machine(session, farm, "No windows", work_rate="2")
    open_machine = _machine(session, farm, "Open", work_rate="2")
    _window(session, farm, machine=open_machine)
    slow = _machine(session, farm, "Too slow", work_rate="0.001")
    _window(session, farm, machine=slow)
    span = engine.Span(job.scheduled_start, job.scheduled_start + timedelta(minutes=90))
    area = parcels[0].area_ha
    open_state = engine.State("open", "open")
    available = engine.State("available", "available")
    offers_by_name = {
        row["name"]: row
        for row in offers._machine_offers(
            repo,
            job,
            "Europe/Paris",
            span,
            area,
            None,
            weather=open_state,
            operator_state=available,
            busy_day=False,
        )
    }
    assert offers_by_name["No windows"]["status"] == "unknown"
    assert offers_by_name["Too slow"]["status"] == "unavailable"
    assert "capacity" in offers_by_name["Too slow"]["reason"]
    unknown_weather = {
        row["name"]: row
        for row in offers._machine_offers(
            repo,
            job,
            "Europe/Paris",
            span,
            area,
            None,
            weather=engine.State("unknown", "no forecast"),
            operator_state=available,
            busy_day=False,
        )
    }
    assert unknown_weather["Open"]["status"] == "unknown"
    assert "Weather" in unknown_weather["Open"]["reason"]
    assert unknown.id != open_machine.id
    session.execute(delete(PlanningJobParcel).where(PlanningJobParcel.job_id == job.id))
    assert offers._job_parcels(repo, job.id) == []


def test_time_offer_without_configured_windows_and_capacity_none(session, farm, parcels, products):
    _forecast(session, farm)
    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    forecast = offers.forecast_context(repo, NOW, "Europe/Paris")
    machine = _machine(session, farm, "No window machine", work_rate="2")
    operator = _operator(session, farm, "No window operator")
    assert offers._capacity_json(None) is None
    assert (
        offers._time_offer(
            repo,
            job,
            NOW,
            "Europe/Paris",
            forecast,
            {"id": machine.id, "work_rate_ha_per_hour": 2.0},
            {"id": operator.id},
            engine.CapacityState("fits", "fits", 10),
            parcels[0].area_ha,
            sulphur=False,
            busy_days=set(),
        )
        is None
    )


def test_parcel_offers_limited_and_undated_advice(session, farm, parcels, products):
    from tests.apps.share.conftest import make_parcel

    repo = _repo(session, farm)
    job = _job(session, farm, parcels[0], products["copper-contact"])
    limited = _advice(session, farm, parcels[1], decision="spray")
    limited.props = {**limited.props, "assessment_status": "limited"}
    third = make_parcel(session, farm, 3)
    _advice(session, farm, third, decision="spray")
    session.flush()
    span = engine.Span(job.scheduled_start, job.scheduled_start + timedelta(minutes=90))
    by_alias = {row["alias"]: row for row in offers._parcel_offers(repo, job, NOW, span)}
    assert by_alias["P2"]["status"] == "not_eligible"
    assert "limited" in by_alias["P2"]["reason"]
    assert by_alias["P3"]["status"] == "not_eligible"
    assert "dated spray window" in by_alias["P3"]["reason"]


def test_job_offers_stop_after_three_feasible_jobs(session, farm, parcels, products, advice):
    _forecast(session, farm)
    repo = _repo(session, farm)
    target = _job(session, farm, parcels[0], products["copper-contact"])
    for index in range(4):
        machine = _machine(session, farm, f"Fleet {index}", work_rate="2")
        operator = _operator(session, farm, f"Crew {index}")
        _window(session, farm, machine=machine)
        _window(session, farm, operator=operator)
        job = service.create(
            repo,
            NOW,
            "Europe/Paris",
            name=f"Other {index}",
            parcel_ids=[parcels[0].id],
            assessment_id=advice.id,
            product_id=products["copper-contact"].id,
            scheduled_start=target.scheduled_start,
            duration_minutes=90,
        )
        job = service.set_assignments(
            repo, NOW, "Europe/Paris", job, expected_revision=1, changes={"machine": machine.id}
        )
        service.set_assignments(
            repo,
            NOW,
            "Europe/Paris",
            job,
            expected_revision=job.revision,
            changes={"operator": operator.id},
        )
    forecast = offers.forecast_context(repo, NOW, "Europe/Paris")
    span = engine.Span(target.scheduled_start, target.scheduled_start + timedelta(minutes=90))
    assert len(offers._job_offers(repo, target, NOW, "Europe/Paris", forecast, span, set())) == 3


def test_engine_guards_and_states():
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.Span(datetime(2024, 4, 9, 6), NOW)
    with pytest.raises(ValueError, match="end after it starts"):
        engine.Span(NOW, NOW)
    with pytest.raises(ValueError, match="availability window"):
        engine.Window(NOW, NOW, True)
    assert engine.capacity_state(0, area_ha=1, duration_minutes=60).status == "unknown"
    assert engine.capacity_state(1, area_ha=-1, duration_minutes=60).status == "unknown"
    assessment = engine.assess(
        engine.State("open", "open"),
        None,
        None,
        engine.CapacityState("too_short", "capacity", 100),
    )
    assert assessment.status == "infeasible"
    config = AdvisorConfig.for_region_year("FRI12", 2024)
    windy = [HourlyObs(ts=NOW, rr1_mm=0.0, t_c=15.0, rh_pct=80.0, ff_kmh=200.0)]
    state = engine.weather_state(windy, engine.Span(NOW, NOW + timedelta(hours=1)), config)
    assert state.status == "closed" and "wind" in state.reason
    span = engine.next_feasible_span(
        series=[
            HourlyObs(ts=NOW + timedelta(hours=i), rr1_mm=0, t_c=15, rh_pct=80, ff_kmh=0)
            for i in range(0, 10)
        ],
        now=NOW,
        tz="Europe/Paris",
        config=config,
        earliest=NOW,
        duration_minutes=60,
        horizon_hours=6,
        machine_bookings=[engine.Span(NOW, NOW + timedelta(hours=1))],
    )
    assert span is not None and span.start >= NOW + timedelta(hours=1)
    assert (
        engine.next_feasible_span(
            series=[
                HourlyObs(ts=NOW + timedelta(hours=i), rr1_mm=0, t_c=15, rh_pct=80, ff_kmh=0)
                for i in range(0, 10)
            ],
            now=NOW,
            tz="Europe/Paris",
            config=config,
            earliest=NOW,
            duration_minutes=60,
            horizon_hours=2,
            work_rate=Decimal("0.001"),
            area_ha=Decimal("100"),
        )
        is None
    )
