"""Farmer availability is farm-scoped server state, not a browser cookie."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.forecast.models import ForecastAvailability
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.main import create_app


def _state(session, farm_id, day="2024-04-09"):
    session.expire_all()
    row = session.scalar(
        select(ForecastAvailability).where(
            ForecastAvailability.farm_id == farm_id,
            ForecastAvailability.day == date.fromisoformat(day),
        )
    )
    return None if row is None else row.available


def test_busy_and_free_are_persisted_per_farm_and_shared_by_the_plan(client, session, farm):
    response = client.post(
        "/forecast/busy",
        data={"day": "2024-04-09", "busy": "1", "next": "/forecast/plan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _state(session, farm.id) is False
    assert "farmcore_forecast_busy" not in client.cookies
    plan = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-09"})
    assert plan.status_code == 200
    assert "Busy" in plan.text and "This day is unavailable" in plan.text

    # Repeating the same choice is idempotent and keeps one row.
    assert (
        client.post(
            "/forecast/busy",
            data={"day": "2024-04-09", "busy": "1", "next": "/forecast/plan"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    session.expire_all()
    assert (
        session.scalar(
            select(ForecastAvailability).where(ForecastAvailability.farm_id == farm.id)
        ).available
        is False
    )

    response = client.post(
        "/forecast/busy",
        data={"day": "2024-04-09", "busy": "0", "next": "/forecast/plan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _state(session, farm.id) is True
    plan = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-09"})
    assert "Availability</dt><dd>Available" in plan.text


def test_availability_does_not_leak_between_farms(client, session, farm):
    other = Farm(name="Another availability farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    client.post(
        "/forecast/busy",
        params={"farm": farm.id},
        data={"day": "2024-04-09", "busy": "1", "farm": str(farm.id)},
        follow_redirects=False,
    )
    assert _state(session, farm.id) is False
    assert _state(session, other.id) is None
    page = client.get("/forecast/plan", params={"farm": other.id, "day": "2024-04-09"})
    assert "Availability</dt><dd>Available" in page.text


def test_availability_validation_and_owner_authority(client, session, settings, farm):
    for params in (
        {"day": "1999-12-31", "busy": "1"},
        {"day": "2024-04-08", "busy": "1"},
        {"day": "bad", "busy": "1"},
        {"day": "2024-04-09", "busy": "yes"},
        {"day": "2024-04-09"},
    ):
        assert client.post("/forecast/busy", data=params).status_code == 422

    demo = create_app(
        settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm.id]}),
        session_factory=client.app.state.session_factory,
    )
    with TestClient(demo) as anonymous:
        denied = anonymous.post(
            "/forecast/busy",
            data={"day": "2024-04-09", "busy": "1"},
            follow_redirects=False,
        )
        assert denied.status_code == 403
    assert _state(session, farm.id) is None
