"""Concurrent completion: one operation, one stock movement, one completion revision."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.inventory.service import stock_rows
from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.planning.models import PlanningJob, PlanningJobRevision
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm, Log, Quantity
from farmcore.main import create_app
from tests.apps.planning.conftest import NOW, job_payload
from tests.apps.share.conftest import make_parcel


def _setup(settings, factory):
    with factory.begin() as session:
        farm = Farm(name="Concurrent completion farm", props={"region": "FRI12"})
        session.add(farm)
        session.flush()
        ensure_owner_grant(session, farm)
        parcel = make_parcel(session, farm, 1)
        products = ensure_products(session)
        session.add(
            InventoryStock(
                farm_id=farm.id,
                product_id=products["copper-contact"].id,
                season=NOW.year,
                unit="kg",
                purchased=Decimal("10"),
            )
        )
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
        farm_id, parcel, products, advice = farm.id, parcel, products, advice
        payload = job_payload([parcel], products, advice)

    app = create_app(settings, session_factory=factory)
    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    with TestClient(app, headers=headers) as browser:
        created = browser.post("/planning/jobs", params={"farm": farm_id}, json=payload)
        assert created.status_code == 201, created.text
        job = created.json()
    return app, headers, farm_id, products, job


def _body(job, key):
    return {
        "expected_revision": job["revision"],
        "request_key": key,
        "actual_start": (NOW - timedelta(minutes=75)).isoformat(),
        "duration_minutes": 60,
        "parcel_ids": [p["id"] for p in job["parcels"]],
        "quantities": [{"product_id": job["product"]["id"], "value": "1.5", "unit": "kg/ha"}],
        "notes": "Concurrent completion",
    }


def _complete(app, headers, farm_id, job_id, key, body):
    with TestClient(app, headers=headers) as browser:
        return browser.post(
            f"/planning/jobs/{job_id}/complete", params={"farm": farm_id}, json=body
        )


def _assert_one_completion(factory, farm_id, products, job_id):
    with factory() as session:
        job = session.get(PlanningJob, job_id)
        assert job.status == "completed"
        assert job.revision == 2
        assert job.operation_log_id is not None
        assert session.scalar(select(func.count()).select_from(Log)) == 1
        assert session.scalar(select(func.count()).select_from(Quantity)) == 1
        revisions = session.scalars(
            select(PlanningJobRevision)
            .where(PlanningJobRevision.job_id == job_id)
            .order_by(PlanningJobRevision.revision)
        ).all()
        assert [(r.revision, r.action) for r in revisions] == [
            (1, "created"),
            (2, "completed"),
        ]
        repo = ScopedRepository(
            session, farm_id, ensure_owner_grant(session, session.get(Farm, farm_id)), "t", "t"
        )
        rows = stock_rows(repo, now=NOW + timedelta(hours=4), tz="Europe/Paris")
        row = {r.product.id: r for r in rows}[products["copper-contact"].id]
        assert (row.sprays, row.on_hand) == (1, Decimal("8.5"))


def test_two_double_taps_with_one_key_create_one_operation(settings, committed_session_factory):
    app, headers, farm_id, products, job = _setup(settings, committed_session_factory)
    body = _body(job, "double-tap-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: _complete(app, headers, farm_id, job["id"], "double-tap-key", body),
                range(2),
            )
        )

    assert sorted(r.status_code for r in responses) == [200, 200]
    assert {r.json()["operation_log_id"] for r in responses} == {
        responses[0].json()["operation_log_id"]
    }
    _assert_one_completion(committed_session_factory, farm_id, products, job["id"])


def test_two_concurrent_completions_with_different_keys_have_one_winner(
    settings, committed_session_factory
):
    app, headers, farm_id, products, job = _setup(settings, committed_session_factory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda key: _complete(app, headers, farm_id, job["id"], key, _body(job, key)),
                ("first-key", "second-key"),
            )
        )

    assert sorted(r.status_code for r in responses) == [200, 409]
    _assert_one_completion(committed_session_factory, farm_id, products, job["id"])
