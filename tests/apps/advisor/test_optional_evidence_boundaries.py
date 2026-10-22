"""Historical advice with optional evidence still has an honest, readable HTTP card.

Synthetic stored rows exercise the card contract without invoking the agronomy engine or
loading any external datasets. Expected words are the plain-language card contract.
"""

from __future__ import annotations

import html

import pytest

from farmcore.core.models.canonical import Advice, Parcel
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.advisor.test_card_plain import by_id
from tests.apps.chat.test_handlers import NOW_LOCAL, seed_farm


@pytest.fixture
def stored_card(session, settings):
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    complete_station(session, parcel.farm_id, NOW_LOCAL)

    def store(decision="none", *, reasons=(), products=(), window=None):
        row = Advice(
            farm_id=parcel.farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW_LOCAL,
            decision=decision,
            reasons=list(reasons),
            products=list(products),
            next_window_start=window[0] if window else None,
            next_window_end=window[1] if window else None,
            props={"headline": "Synthetic stored advice"},
        )
        session.add(row)
        session.commit()
        return f"/advisor/parcels/{parcel.id}"

    return store


def reason(rule, *, inputs=None, threshold=None, verdict="checked"):
    return {
        "rule": rule,
        "verdict": verdict,
        "source": "Synthetic historical evidence",
        "inputs": inputs or {},
        "threshold": threshold or {},
    }


@pytest.mark.parametrize(
    ("reasons", "products", "expected"),
    [
        (
            [
                reason(
                    "downy_3_10",
                    inputs={"active_events": ["2024-04-09T04:00:00+00:00"]},
                    threshold={"downy_temp_min_c": 10, "downy_rain_min_mm": 10},
                )
            ],
            [{"name": "Copper"}],
            "Spray Copper, because an infection event → downy mildew risk",
        ),
        ([], [{"name": "Sulphur"}], "Spray Sulphur, because the powdery mildew cover has ended"),
        ([], [], "Spray, because no pass is due"),
    ],
)
def test_stored_spray_without_a_window_explains_the_available_evidence(
    client, stored_card, reasons, products, expected
):
    response = client.get(stored_card("spray", reasons=reasons, products=products))

    assert response.status_code == 200
    assert by_id(response.text, "lead")["text"] == expected
    assert "No spray window in the forecast" in response.text


@pytest.mark.parametrize(
    ("horizon", "words"),
    [(None, "in the forecast"), (24, "in the next 1 day"), (36, "in the next 36 h")],
)
def test_wait_without_a_window_never_invents_a_deadline(client, stored_card, horizon, words):
    threshold = {} if horizon is None else {"window_horizon_h": horizon}
    response = client.get(
        stored_card("wait", reasons=[reason("spray_window", threshold=threshold)])
    )

    assert response.status_code == 200
    assert by_id(response.text, "lead")["text"] == f"Wait. No spray window {words}"
    assert f"No spray window {words}" in html.unescape(response.text)


def test_wait_window_starting_tomorrow_uses_the_local_day(client, stored_card):
    response = client.get(
        stored_card("wait", window=(NOW_LOCAL.replace(day=11), NOW_LOCAL.replace(day=11, hour=10)))
    )

    assert response.status_code == 200
    assert (
        by_id(response.text, "lead")["text"]
        == "Wait. Next window tomorrow from 06:00, before 10:00"
    )
    assert "Tomorrow from 06:00, before 10:00" in response.text


def test_protected_without_an_end_date_keeps_the_stored_decision(client, stored_card):
    response = client.get(stored_card("protected"))

    assert response.status_code == 200
    assert by_id(response.text, "lead")["text"] == "Protected"
    assert "Not needed: protected" in response.text
    assert "Protected until" not in response.text


def test_unknown_washoff_amount_is_not_rendered_as_zero_rain(client, stored_card):
    response = client.get(
        stored_card("wait", reasons=[reason("protection_downy", verdict="washed off")])
    )

    assert response.status_code == 200
    assert by_id(response.text, "protection")["text"].endswith(
        "Not protected: the last spray was washed off by rain"
    )
    assert 'id="protection-bar"' not in response.text
    assert "0.0 mm" not in response.text


def test_boolean_historical_threshold_is_readable_in_farmer_details(client, stored_card):
    response = client.get(
        stored_card(reasons=[reason("legacy_check", threshold={"enabled": True})])
    )

    assert response.status_code == 200
    detail = by_id(response.text, "why")["text"]
    assert "Enabled True" in detail
    assert "Enabled 1" not in detail


def test_projected_symptoms_inside_forecast_remain_explicitly_projected(client, stored_card):
    response = client.get(
        stored_card(
            reasons=[
                reason(
                    "downy_incubation",
                    inputs={
                        "event": "2024-04-09T04:00:00+00:00",
                        "pct": 50,
                        "expected_symptoms": "2024-04-11T04:00:00+00:00",
                    },
                )
            ]
        )
        + "?debug=1"
    )

    assert response.status_code == 200
    assert "Symptoms expected around 11 Apr (projected)" in response.text
    assert "beyond the forecast" not in response.text


def test_before_season_card_explains_why_no_action_is_needed(client, stored_card):
    response = client.get(
        stored_card(
            reasons=[
                reason(
                    "season_gate",
                    verdict="season not started",
                    threshold={"season_gate": "2024-04-15"},
                )
            ]
        )
    )

    assert response.status_code == 200
    assert by_id(response.text, "lead")["text"] == (
        "Nothing to do today: the season has not started"
    )
    assert "expected 15 Apr 2024" in response.text
