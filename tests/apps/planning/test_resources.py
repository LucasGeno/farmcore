"""Machines, operators and their availability windows are farm-scoped configured state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.planning.models import (
    PlanningMachine,
    PlanningOperator,
    PlanningResourceWindow,
)
from farmcore.core.access import DataGrant, ScopeDenied, ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.main import create_app


def _machine(session, farm, name="Sprayer 1", **overrides):
    payload = {
        "farm_id": farm.id,
        "name": name,
        "kind": "sprayer",
        "work_rate_ha_per_hour": None,
        "tank_litres": None,
        "active": True,
        "notes": "",
        "created_by": "owner",
        "updated_by": "owner",
    }
    payload.update(overrides)
    row = PlanningMachine(**payload)
    session.add(row)
    session.flush()
    return row


def test_owner_configures_machines_operators_and_windows(client, session, farm):
    machine = client.post(
        "/planning/resources/machines-form",
        params={"farm": farm.id},
        data={
            "name": "Sprayer 1",
            "kind": "sprayer",
            "work_rate_ha_per_hour": "1.5",
            "tank_litres": "600",
            "notes": "Main sprayer",
        },
        follow_redirects=False,
    )
    assert machine.status_code == 303
    assert machine.headers["location"] == f"/planning/?farm={farm.id}"

    operator = client.post(
        "/planning/resources/operators-form",
        params={"farm": farm.id},
        data={"name": "Ana", "notes": "Driver"},
        follow_redirects=False,
    )
    assert operator.status_code == 303

    session.expire_all()
    configured_machine = session.scalar(select(PlanningMachine))
    configured_operator = session.scalar(select(PlanningOperator))
    assert configured_machine.name == "Sprayer 1"
    assert configured_machine.kind == "sprayer"
    assert float(configured_machine.work_rate_ha_per_hour) == pytest.approx(1.5)
    assert configured_operator.name == "Ana"

    window = client.post(
        "/planning/resources/windows-form",
        params={"farm": farm.id},
        data={
            "resource": f"machine:{configured_machine.id}",
            "starts_at": "2024-04-10T08:00",
            "ends_at": "2024-04-10T12:00",
            "available": "1",
            "notes": "Morning shift",
        },
        follow_redirects=False,
    )
    assert window.status_code == 303
    session.expire_all()
    row = session.scalar(select(PlanningResourceWindow))
    assert (row.machine_id, row.operator_id) == (configured_machine.id, None)
    assert row.available is True
    assert row.starts_at == datetime(2024, 4, 10, 6, tzinfo=UTC)  # 08:00 Paris

    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert "Sprayer 1" in page.text and "Ana" in page.text
    assert "Morning shift" in page.text


def test_unknown_machine_capacity_stays_unknown(client, session, farm):
    response = client.post(
        "/planning/resources/machines-form",
        params={"farm": farm.id},
        data={
            "name": "Hire sprayer",
            "kind": "sprayer",
            "work_rate_ha_per_hour": "",
            "tank_litres": "",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    machine = session.scalar(select(PlanningMachine).where(PlanningMachine.name == "Hire sprayer"))
    assert machine.work_rate_ha_per_hour is None
    assert machine.tank_litres is None
    page = client.get("/planning/", params={"farm": farm.id})
    assert "capacity unknown" in page.text.lower()


def test_resource_forms_reject_invalid_values_and_foreign_ids(client, session, farm):
    other = Farm(name="Other resource farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    foreign = _machine(session, other, name="Foreign sprayer")

    for data in (
        {"name": "", "kind": "sprayer"},
        {"name": "Bad kind", "kind": "spaceship"},
        {"name": "No capacity", "kind": "sprayer", "work_rate_ha_per_hour": "0"},
    ):
        response = client.post(
            "/planning/resources/machines-form",
            params={"farm": farm.id},
            data=data,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    for data in (
        {
            "resource": f"machine:{foreign.id}",
            "starts_at": "2024-04-10T08:00",
            "ends_at": "2024-04-10T12:00",
            "available": "1",
        },
        {
            "resource": "machine:999999",
            "starts_at": "2024-04-10T08:00",
            "ends_at": "2024-04-10T12:00",
            "available": "1",
        },
        {
            "resource": "machine:1",
            "starts_at": "2024-04-10T12:00",
            "ends_at": "2024-04-10T08:00",
            "available": "1",
        },
        {
            "resource": "tractor:1",
            "starts_at": "2024-04-10T08:00",
            "ends_at": "2024-04-10T12:00",
            "available": "1",
        },
    ):
        response = client.post(
            "/planning/resources/windows-form",
            params={"farm": farm.id},
            data=data,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    session.expire_all()
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningMachine)
            .where(PlanningMachine.farm_id == farm.id)
        )
        == 0
    )
    assert session.scalar(select(func.count()).select_from(PlanningResourceWindow)) == 0


def test_a_grant_without_planning_read_cannot_read_resources(session, farm):
    limited = DataGrant(farm_id=farm.id, grantee="share:1", scopes=["fields:read"], purpose="share")
    session.add(limited)
    session.flush()
    repo = ScopedRepository(session, farm.id, limited, "share", "test")
    with pytest.raises(ScopeDenied):
        repo.list(PlanningMachine, scope="fields:read")
    owner = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")
    assert owner.list(PlanningMachine, scope="planning:read") == []


def test_anonymous_demo_visitor_cannot_read_resources(client, settings, session, farm):
    _machine(session, farm)
    session.flush()
    demo = create_app(
        settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm.id]}),
        session_factory=client.app.state.session_factory,
    )
    with TestClient(demo) as anonymous:
        page = anonymous.get("/planning/", params={"farm": farm.id})
        assert page.status_code == 403
        jobs = anonymous.get("/planning/jobs.json", params={"farm": farm.id})
        assert jobs.status_code == 403
        assert "Sprayer" not in jobs.text


def test_owner_deactivates_a_machine_and_it_cannot_be_booked(
    client, session, farm, parcels, products, advice
):
    machine = _machine(session, farm)
    created = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json={
            "name": "Spray",
            "parcel_ids": [parcels[0].id],
            "assessment_id": advice.id,
            "product_id": products["copper-contact"].id,
            "scheduled_start": advice.next_window_start.isoformat(),
            "duration_minutes": 90,
            "notes": "",
        },
    )
    assert created.status_code == 201
    response = client.post(
        f"/planning/resources/machine/{machine.id}/active-form",
        params={"farm": farm.id},
        data={"active": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.get(PlanningMachine, machine.id).active is False
    refused = client.post(
        f"/planning/jobs/{created.json()['id']}/assignments",
        params={"farm": farm.id},
        json={"expected_revision": 1, "kind": "machine", "resource_id": machine.id},
    )
    assert refused.status_code == 422


def test_demo_reset_clears_configured_resources(client, session, farm):
    from farmcore.core import demo_control, registry

    machine = _machine(session, farm)
    operator = PlanningOperator(
        farm_id=farm.id,
        name="Ana",
        created_by="owner",
        updated_by="owner",
    )
    session.add(operator)
    session.flush()
    session.add(
        PlanningResourceWindow(
            farm_id=farm.id,
            machine_id=machine.id,
            starts_at=datetime(2024, 4, 10, 6, tzinfo=UTC),
            ends_at=datetime(2024, 4, 10, 10, tzinfo=UTC),
            available=True,
            notes="",
            created_by="owner",
            updated_by="owner",
        )
    )
    session.flush()
    session.expire_all()

    demo_control._clear(session, farm, registry.discover())
    session.flush()
    session.expire_all()

    for model in (PlanningMachine, PlanningOperator, PlanningResourceWindow):
        assert session.scalar(select(func.count()).select_from(model)) == 0
