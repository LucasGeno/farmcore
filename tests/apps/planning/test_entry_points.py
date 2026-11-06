"""Stored advice and the forecast plan are entry points to a persistent planned job."""

from __future__ import annotations

from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.planning.conftest import NOW


def test_forecast_plan_and_advice_card_offer_a_job_link(
    client, session, farm, parcels, products, advice
):
    complete_station(session, farm.id, NOW)
    session.flush()
    plan = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-09"})
    assert plan.status_code == 200
    assert f"/planning/new?parcel={parcels[0].id}" in plan.text
    assert f"assessment={advice.id}" in plan.text

    card = client.get(f"/advisor/parcels/{parcels[0].id}", params={"farm": farm.id})
    assert card.status_code == 200
    assert "/planning/new?parcel=" in card.text
    assert f"assessment={advice.id}" in card.text
