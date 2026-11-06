"""Saved availability is farm-member state; anonymous demo readers see every day available."""

from __future__ import annotations

from fastapi.testclient import TestClient

from farmcore.core import clock
from farmcore.main import create_app
from tests.apps.forecast.test_routes import _days, _utc
from tests.apps.forecast.test_routes import imported as imported
from tests.apps.forecast.test_routes import parcel as parcel


def _demo(settings, client, farm):
    return create_app(
        settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm.id]}),
        session_factory=client.app.state.session_factory,
    )


def _mark_busy(client, farm, day):
    response = client.post(
        "/forecast/busy",
        params={"farm": farm.id},
        data={"day": day, "busy": "1", "next": "/forecast/plan", "farm": str(farm.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_anonymous_demo_plan_today_and_parcel_ignore_saved_busy_days(
    client, session, settings, farm, parcel, imported
):
    clock.set_replay(session, _utc(9, 4), settings)
    session.commit()
    _mark_busy(client, farm, "2024-04-09")
    admin_plan = dict(_days(client.get("/forecast/plan", params={"farm": farm.id}).text))
    assert admin_plan["2024-04-09"] == "busy"

    with TestClient(_demo(settings, client, farm)) as anon:
        plan = anon.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-09"})
        assert plan.status_code == 200
        assert dict(_days(plan.text))["2024-04-09"] != "busy"
        assert "This day is unavailable" not in plan.text
        assert "Availability</dt><dd>Available" in plan.text

        today = anon.get("/forecast/today", params={"farm": farm.id})
        assert today.status_code == 200
        assert ("2024-04-09", "best") in _days(today.text)

        parcel_window = anon.get(f"/forecast/parcels/{parcel.id}", params={"farm": farm.id})
        assert parcel_window.status_code == 200
        assert dict(_days(parcel_window.text))["2024-04-09"] != "busy"


def test_anonymous_demo_assistant_plan_ignores_saved_busy_days(
    client, session, settings, farm, imported
):
    clock.set_replay(session, _utc(9, 4), settings)
    session.commit()
    _mark_busy(client, farm, "2024-04-09")
    admin = client.get(
        "/assistant/ask", params={"q": "Which days this week are good?", "farm": farm.id}
    ).json()
    assert admin["tool"] == "assistant_plan"
    assert any(d["day"] == "2024-04-09" and d["grade"] == "busy" for d in admin["facts"]["days"])

    with TestClient(_demo(settings, client, farm)) as anon:
        body = anon.get(
            "/assistant/ask", params={"q": "Which days this week are good?", "farm": farm.id}
        ).json()
        assert body["tool"] == "assistant_plan"
        assert any(d["day"] == "2024-04-09" for d in body["facts"]["days"])
        assert not any(
            d["day"] == "2024-04-09" and d["grade"] == "busy" for d in body["facts"]["days"]
        )


def test_anonymous_demo_notifications_ignore_saved_busy_days(
    client, session, settings, farm, imported
):
    clock.set_replay(session, _utc(9, 4), settings)
    session.commit()
    _mark_busy(client, farm, "2024-04-12")
    admin = client.get("/notifications", params={"farm": farm.id}).text
    assert "Spray window: avoid" not in admin

    with TestClient(_demo(settings, client, farm)) as anon:
        body = anon.get("/notifications", params={"farm": farm.id}).text
        assert "Spray window: avoid" in body
