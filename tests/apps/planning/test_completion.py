"""Completing a planned job: preview, one confirmed operation, retries and rollback."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.inventory.service import stock_rows
from farmcore.apps.planning.models import PlanningJob, PlanningJobRevision
from farmcore.core import logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Log, LogSource, Quantity
from tests.apps.planning.conftest import NOW, job_payload


def _created(client, farm, parcels, products, advice, **overrides):
    response = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def completion_body(job, *, parcel_ids=None, quantities=None, **overrides):
    body = {
        "expected_revision": job["revision"],
        "request_key": "completion-key-1",
        "actual_start": job["scheduled_start"],
        "duration_minutes": job["duration_minutes"],
        "parcel_ids": parcel_ids or [p["id"] for p in job["parcels"]],
        "quantities": quantities
        or [{"product_id": job["product"]["id"], "value": "1.5", "unit": "kg/ha"}],
        "notes": "Completed as planned",
    }
    body.update(overrides)
    return body


def _stock(session, farm, products, purchased="10"):
    row = InventoryStock(
        farm_id=farm.id,
        product_id=products["copper-contact"].id,
        season=NOW.year,
        unit="kg",
        purchased=Decimal(purchased),
    )
    session.add(row)
    session.flush()
    return row


def _usage(session, farm):
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")
    return {
        row.product.id: row
        for row in stock_rows(repo, now=NOW + timedelta(hours=4), tz="Europe/Paris")
    }


def test_completion_preview_shows_actual_differences_and_writes_nothing(
    client, session, farm, parcels, products, advice
):
    created = _created(client, farm, parcels, products, advice)
    preview = client.post(
        f"/planning/jobs/{created['id']}/completion/preview",
        params={"farm": farm.id},
        json=completion_body(
            created,
            actual_start=(NOW + timedelta(hours=1)).isoformat(),
            duration_minutes=60,
            parcel_ids=[p.id for p in parcels],
            quantities=[
                {"product_id": products["copper-contact"].id, "value": "2", "unit": "kg/ha"}
            ],
            notes="Pass was slower and covered the second block",
        ),
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["job_id"] == created["id"]
    assert body["revision"] == 1
    assert body["plan_revision"] == 1
    assert body["assessment"]["id"] == advice.id
    assert body["assessment"]["config_version"]["status"] == "unreviewed"
    assert body["planned"]["duration_minutes"] == 90
    assert body["actual"]["duration_minutes"] == 60
    assert body["actual"]["parcel_ids"] == sorted([p.id for p in parcels])
    assert body["actual"]["quantities"][0]["value"] == "2"
    assert any("Duration" in line for line in body["differences"])
    assert any("P2" in line for line in body["differences"])
    assert body["availability"] == {"day": "2024-04-09", "available": True}

    session.expire_all()
    job = session.get(PlanningJob, created["id"])
    assert (job.status, job.revision, job.completed_at) == ("planned", 1, None)
    assert job.operation_log_id is None
    assert session.scalar(select(func.count()).select_from(Log)) == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == job.id)
        )
        == 1
    )


def test_completion_creates_one_confirmed_operation_and_retries_are_idempotent(
    client, session, farm, parcels, products, advice
):
    _stock(session, farm, products)
    created = _created(client, farm, parcels, products, advice)
    body = completion_body(
        created,
        actual_start=NOW.isoformat(),
        duration_minutes=60,
        parcel_ids=[p.id for p in parcels],
        quantities=[{"product_id": products["copper-contact"].id, "value": "1.5", "unit": "kg/ha"}],
    )

    response = client.post(
        f"/planning/jobs/{created['id']}/complete", params={"farm": farm.id}, json=body
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "completed"
    assert job["revision"] == 2
    assert job["completed_by"] == "owner"
    assert job["completed_at"] is not None
    assert job["operation_log_id"] is not None
    assert job["completion"]["duration_minutes"] == 60
    assert job["completion"]["operation_log_id"] == job["operation_log_id"]
    assert job["operation"]["status"] == "done"
    assert job["operation"]["source_types"] == ["planning"]

    session.expire_all()
    saved = session.get(PlanningJob, created["id"])
    operation = session.get(Log, job["operation_log_id"])
    assert (saved.status, saved.completed_by, saved.operation_request_key) == (
        "completed",
        "owner",
        "completion-key-1",
    )
    assert operation.status == "done"
    assert operation.type == "spray"
    assert operation.ts_start == NOW
    assert operation.ts_end == NOW + timedelta(hours=1)
    assert {p.id for p in operation.parcels} == {p.id for p in parcels}
    assert [(p.product_id, str(p.value), p.unit) for p in operation.quantities] == [
        (products["copper-contact"].id, "1.5", "kg/ha")
    ]
    source = session.scalar(select(LogSource).where(LogSource.log_id == operation.id))
    assert source.source_type == "planning"
    assert source.external_key == f"job:{created['id']}:completion-key-1"
    assert source.raw_payload["assessment_id"] == advice.id
    assert source.raw_payload["plan_revision"] == 1
    from farmcore.apps.operations.spray_weather import NAMESPACE

    receipt = source.raw_payload[NAMESPACE]
    assert receipt["parcel_ids"] == sorted(p.id for p in parcels)
    assert receipt["source_time"].startswith("2024-04-09T04:00")
    assert receipt["policy"] == "exact_previous_hour_slot_v1"

    revisions = session.scalars(
        select(PlanningJobRevision)
        .where(PlanningJobRevision.job_id == created["id"])
        .order_by(PlanningJobRevision.revision)
    ).all()
    assert [(r.revision, r.action, r.status) for r in revisions] == [
        (1, "created", "planned"),
        (2, "completed", "completed"),
    ]
    snapshot = revisions[1].snapshot
    assert snapshot["status"] == "completed"
    assert snapshot["scheduled_start"].startswith("2024-04-09T06:00")
    assert snapshot["duration_minutes"] == 90  # the saved plan stays the plan
    assert snapshot["completion"]["duration_minutes"] == 60
    assert snapshot["completion"]["operation_log_id"] == operation.id
    assert snapshot["completion"]["notes"] == "Completed as planned"
    assert snapshot["availability"] == {"day": "2024-04-09", "available": True}

    usage = _usage(session, farm)[products["copper-contact"].id]
    assert (usage.sprays, usage.used, usage.on_hand) == (1, Decimal("3.375"), Decimal("6.625"))

    # A retried double tap with the same key returns the same operation.
    retry = client.post(
        f"/planning/jobs/{created['id']}/complete", params={"farm": farm.id}, json=body
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["operation_log_id"] == job["operation_log_id"]
    assert retry.json()["revision"] == 2
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Log)) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == created["id"])
        )
        == 2
    )
    assert _usage(session, farm)[products["copper-contact"].id].on_hand == Decimal("6.625")

    # Reusing the key for different actual work is refused, not silently replayed.
    changed = client.post(
        f"/planning/jobs/{created['id']}/complete",
        params={"farm": farm.id},
        json={**body, "duration_minutes": 30},
    )
    assert changed.status_code == 409
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Log)) == 1

    # A different key cannot complete the terminal job a second time.
    again = client.post(
        f"/planning/jobs/{created['id']}/complete",
        params={"farm": farm.id},
        json=completion_body(created, request_key="another-key"),
    )
    assert again.status_code == 409
    assert session.scalar(select(func.count()).select_from(Log)) == 1

    # Completed work is a confirmed record, not a second planned job.
    diary = client.get("/operations/", params={"farm": farm.id})
    assert diary.status_code == 200 and "Spray block A" in diary.text
    register = client.get("/operations/register", params={"farm": farm.id})
    assert register.status_code == 200
    assert products["copper-contact"].name in register.text
    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200 and "completed" in page.text


def test_completion_merges_into_a_matching_confirmed_spray_once(
    client, session, farm, parcels, products, advice
):
    _stock(session, farm, products)
    existing, _ = logs.record_operation(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[parcels[0].id],
        ts_start=NOW + timedelta(hours=1),
        ts_end=None,
        quantities=[
            logs.QuantityInput(
                measure="rate",
                value=Decimal("1"),
                unit="kg/ha",
                product_id=products["copper-contact"].id,
                label=products["copper-contact"].name,
            )
        ],
        source=logs.SourceInput("file", "machine-file-1", {}, None, logs.RANK_FILE),
        actor="test",
        occurred_at=NOW,
        status="done",
    )
    created = _created(client, farm, parcels, products, advice)

    response = client.post(
        f"/planning/jobs/{created['id']}/complete",
        params={"farm": farm.id},
        json=completion_body(
            created,
            actual_start=(NOW + timedelta(hours=1)).isoformat(),
            quantities=[
                {"product_id": products["copper-contact"].id, "value": "1.5", "unit": "kg/ha"}
            ],
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["operation_log_id"] == existing.id
    assert response.json()["completion"]["merged"] is True

    session.expire_all()
    operation = session.get(Log, existing.id)
    assert session.scalar(select(func.count()).select_from(Log)) == 1
    assert {s.source_type for s in operation.sources} == {"file", "planning"}
    assert [(str(q.value), q.unit) for q in operation.quantities] == [("1.5", "kg/ha")]
    usage = _usage(session, farm)[products["copper-contact"].id]
    assert (usage.sprays, usage.on_hand) == (1, Decimal("8.125"))


def test_a_failed_completion_leaves_plan_log_and_stock_unchanged(
    client, session, farm, parcels, products, advice
):
    _stock(session, farm, products)
    first = _created(client, farm, parcels, products, advice)
    second = _created(client, farm, parcels, products, advice, name="Second block")

    completed = client.post(
        f"/planning/jobs/{first['id']}/complete",
        params={"farm": farm.id},
        json=completion_body(first, request_key="shared-key"),
    )
    assert completed.status_code == 200, completed.text
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Log)) == 1

    # The farm-scoped unique idempotency key is a database constraint: the second job's
    # canonical write must roll back with the job update, leaving plan, log and stock alone.
    failed = client.post(
        f"/planning/jobs/{second['id']}/complete",
        params={"farm": farm.id},
        json=completion_body(second, request_key="shared-key"),
    )
    assert failed.status_code == 409
    session.expire_all()
    saved = session.get(PlanningJob, second["id"])
    assert (saved.status, saved.revision, saved.operation_log_id) == ("planned", 1, None)
    assert session.scalar(select(func.count()).select_from(Log)) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == second["id"])
        )
        == 1
    )
    usage = _usage(session, farm)[products["copper-contact"].id]
    assert (usage.sprays, usage.on_hand) == (1, Decimal("8.125"))

    # An invalid dose never reaches the canonical log either.
    invalid = client.post(
        f"/planning/jobs/{second['id']}/complete",
        params={"farm": farm.id},
        json=completion_body(
            second,
            request_key="valid-key",
            quantities=[
                {"product_id": products["copper-contact"].id, "value": "1", "unit": "cups"}
            ],
        ),
    )
    assert invalid.status_code == 422
    session.expire_all()
    assert session.get(PlanningJob, second["id"]).status == "planned"
    assert session.scalar(select(func.count()).select_from(Log)) == 1


def test_the_completion_form_previews_differences_then_confirms(
    client, session, farm, parcels, products, advice
):
    created = _created(client, farm, parcels, products, advice)
    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert f"/planning/jobs/{created['id']}/complete-preview-form" in page.text
    assert 'name="actual_start"' in page.text
    assert 'name="dose_value"' in page.text

    form = {
        "expected_revision": str(created["revision"]),
        "request_key": "form-key-1",
        "actual_start": "2024-04-09T07:30",
        "duration_minutes": "60",
        "parcel_ids": [str(p.id) for p in parcels],
        "product_id": [str(products["copper-contact"].id)],
        "dose_value": ["1.5"],
        "dose_unit": ["kg/ha"],
        "notes": "Form completed",
    }
    preview = client.post(
        f"/planning/jobs/{created['id']}/complete-preview-form",
        params={"farm": farm.id},
        data=form,
    )
    assert preview.status_code == 200, preview.text
    assert "Confirm completed work" in preview.text
    assert "Duration" in preview.text
    assert 'name="request_key" value="form-key-1"' in preview.text
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Log)) == 0

    confirmed = client.post(
        f"/planning/jobs/{created['id']}/complete-form",
        params={"farm": farm.id},
        data=form,
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == f"/planning/?farm={farm.id}"
    session.expire_all()
    saved = session.get(PlanningJob, created["id"])
    assert (saved.status, saved.revision) == ("completed", 2)
    operation = session.get(Log, saved.operation_log_id)
    assert operation.ts_start == NOW + timedelta(hours=1, minutes=30)
    assert [(q.product_id, str(q.value), q.unit) for q in operation.quantities] == [
        (products["copper-contact"].id, "1.5", "kg/ha")
    ]
    assert session.scalar(select(func.count()).select_from(Quantity)) == 1


def test_demo_reset_removes_a_completed_job_and_its_operation(
    client, session, farm, parcels, products, advice
):
    from farmcore.core import demo_control, registry

    created = _created(client, farm, parcels, products, advice)
    done = client.post(
        f"/planning/jobs/{created['id']}/complete",
        params={"farm": farm.id},
        json=completion_body(created),
    )
    assert done.status_code == 200, done.text

    session.expire_all()
    demo_control._clear(session, farm, registry.discover())
    session.flush()
    session.expire_all()
    assert session.get(PlanningJob, created["id"]) is None
    assert session.scalar(select(func.count()).select_from(Log)) == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobRevision)
            .where(PlanningJobRevision.farm_id == farm.id)
        )
        == 0
    )
