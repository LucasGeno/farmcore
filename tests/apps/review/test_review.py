"""Parameter lifecycle through real HTTP and migrated PostgreSQL."""

import pytest
from fastapi.testclient import TestClient

from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Farm


def test_parameter_catalog_is_explicitly_unreviewed(client, session):
    farm = Farm(name="Review farm", props={"key": "review-farm", "region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    response = client.get("/review/parameters.json", params={"farm": farm.id})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unreviewed"
    assert body["scope"] == {
        "farm_id": farm.id,
        "crop": "vine",
        "region": "FRI12",
        "evaluation_year": 2024,
    }
    rain = body["catalog"]["downy_rain_min_mm"]
    assert (rain["value"], rain["unit"], rain["assumed"]) == (10.0, "mm", False)
    assert (rain["min"], rain["max"]) == (0.1, 200)
    assert rain["source"].startswith("3-10 rule (Baldacci 1947)")
    assert body["catalog"]["season_gate"]["assumed"] is True
    assert not any(entry["modified"] for entry in body["catalog"].values())
    assert body["accuracy_claim"] is False


def test_an_out_of_range_version_id_is_not_found_without_a_server_error(client, session):
    farm = Farm(name="ID boundary farm", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    with TestClient(
        client.app, headers=dict(client.headers), raise_server_exceptions=False
    ) as http:
        response = http.post(f"/review/versions/{2**63}/activate", params={"farm": farm.id})
    assert response.status_code == 404


def test_activation_only_changes_future_assessments_and_later_opinion_is_explicit(
    client, session, settings
):
    from copy import deepcopy
    from datetime import UTC, datetime

    from sqlalchemy import select

    from farmcore.apps.advisor.job import evaluate
    from farmcore.core.manifest import JobContext
    from farmcore.core.models.canonical import Advice, Observation, Parcel
    from tests.apps.advisor.review_fixtures import complete_station
    from tests.apps.chat.test_handlers import seed_farm

    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    station = complete_station(session, parcel.farm_id, now)
    rainy = session.scalar(
        select(Observation).where(
            Observation.asset_id == station.id,
            Observation.variable == "rr1_mm",
            Observation.ts == datetime(2024, 4, 9, 4, tzinfo=UTC),
        )
    )
    rainy.value = 11.0
    session.flush()
    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))
    previous = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )
    assert previous.decision == "wait"
    original = deepcopy(previous.props)
    original_reasons = deepcopy(previous.reasons)
    params = {"farm": parcel.farm_id}
    proposed = client.post(
        "/review/versions",
        params=params,
        json={"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "Bounded trial"},
    )
    assert proposed.status_code == 201
    version = proposed.json()["id"]
    changed = proposed.json()["parameter_catalog"]["downy_rain_min_mm"]
    assert (changed["value"], changed["baseline_value"], changed["modified"]) == (12.0, 10.0, True)
    assert changed["source"].startswith("3-10 rule (Baldacci 1947)")
    assert proposed.json()["status"] == "unreviewed"
    assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 409
    assert (
        client.post(
            f"/review/versions/{version}/reviews",
            params=params,
            json={"verdict": "approved", "note": "Trial only", "evidence": "Desk"},
        ).status_code
        == 201
    )
    assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 201
    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))
    latest = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )
    assert latest.id != previous.id
    assert latest.decision == "none"
    assert latest.props["config_version"]["id"] == version
    detail = client.get(f"/review/versions/{version}.json", params=params).json()
    assert latest.props["config_version"]["hash"] == detail["config_hash"]
    assert detail["effective_config"]["downy_rain_min_mm"]["value"] == 12.0
    assert previous.props == original
    assert previous.reasons == original_reasons
    assert (
        client.post(
            f"/review/versions/{version}/reviews",
            params=params,
            json={"verdict": "rejected", "note": "Trial withdrawn", "evidence": "Followup"},
        ).status_code
        == 201
    )
    assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 409
    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))
    after_rejection = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )
    assert after_rejection.props["config_version"]["status"] == "rejected"
    assert after_rejection.props["assessment_status"] == "limited"
    assert "rejected" in after_rejection.props["headline"]
    assert latest.props["config_version"]["status"] == "approved"
    assert previous.props == original
    for path in ("/advisor/today", "/advisor/today?view=calm", "/advisor/map/farm"):
        view = client.get(path, params=params)
        assert view.status_code == 200
        assert "Assessment limited" in view.text
        assert "Nothing to spray today" not in view.text
        assert "no infection risk right now" not in view.text
        assert "No spray needed" not in view.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"executable_expression": "eval(1)"},
        {"downy_rain_min_mm": True},
        {"downy_rain_min_mm": "12"},
        {"downy_rain_min_mm": 201},
        {"downy_window_h": 48.5},
        {"downy_rain_min_mm": 10**1000},
        {"downy_rain_min_mm": float("nan")},
        {"season_gate": "2024-02-30"},
        {"season_gate": "2025-04-05"},
        {"incub_alert_pct": 90, "incub_symptoms_pct": 50},
        {"region": "ES418"},
        {"downy_rain_min_mm": {"value": 12, "source": "Invented", "assumed": False}},
    ],
)
def test_rejected_parameter_inputs_write_no_versions(client, session, overrides):
    import json

    from sqlalchemy import func, select

    from farmcore.apps.review.models import ParameterVersion

    farm = Farm(name="Validation farm", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    response = client.post(
        "/review/versions",
        params={"farm": farm.id},
        content=json.dumps({"overrides": overrides, "evidence": "Boundary test"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert (
        session.scalar(
            select(func.count())
            .select_from(ParameterVersion)
            .where(ParameterVersion.farm_id == farm.id)
        )
        == 0
    )


def test_cross_farm_version_parent_review_and_activation_never_leak(client, session):
    farms = [
        Farm(name="Scope A", props={"region": "FRI12"}),
        Farm(name="Scope B", props={"region": "FRI12"}),
    ]
    session.add_all(farms)
    session.flush()
    for farm in farms:
        ensure_owner_grant(session, farm)
    a, b = farms
    created = client.post(
        "/review/versions",
        params={"farm": a.id},
        json={"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "PRIVATE FARM A EVIDENCE"},
    )
    assert created.status_code == 201
    version = created.json()["id"]
    for path, body in [
        (
            f"/review/versions/{version}/reviews",
            {"verdict": "approved", "note": "Foreign", "evidence": "Foreign"},
        ),
        (f"/review/versions/{version}/activate", None),
    ]:
        response = client.post(path, params={"farm": b.id}, json=body)
        assert response.status_code == 404
        assert "PRIVATE FARM A" not in response.text
    response = client.get(f"/review/versions/{version}.json", params={"farm": b.id})
    assert response.status_code == 404
    parent = client.post(
        "/review/versions",
        params={"farm": b.id},
        json={
            "parent_version_id": version,
            "overrides": {"downy_rain_min_mm": 13.0},
            "evidence": "Foreign parent",
        },
    )
    assert parent.status_code == 422
    assert "PRIVATE FARM A" not in parent.text
    assert (
        client.get("/review/parameters.json", params={"farm": b.id}).json()["catalog"][
            "downy_rain_min_mm"
        ]["value"]
        == 10.0
    )


def test_active_parameters_are_explained_and_used_without_claiming_missing_agronomy(
    client, session
):
    farm = Farm(name="Effective explanations", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    from farmcore.apps.forecast import sources, store
    from tests.apps.forecast.conftest import FIXTURE

    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    from datetime import UTC, datetime

    from farmcore.core.clock import set_replay

    set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), client.app.state.settings)
    params = {"farm": farm.id}
    proposed = client.post(
        "/review/versions",
        params=params,
        json={
            "overrides": {"downy_rain_min_mm": 12.0, "calendar.window_day_start_hour_local": 8},
            "evidence": "Bounded local opinion",
        },
    )
    assert proposed.status_code == 201
    version_id = proposed.json()["id"]
    assert (
        client.post(
            f"/review/versions/{version_id}/reviews",
            params=params,
            json={"verdict": "approved", "note": "Trial opinion", "evidence": "Desk"},
        ).status_code
        == 201
    )
    assert client.post(f"/review/versions/{version_id}/activate", params=params).status_code == 201
    about = client.get("/advisor/about", params=params)
    assert about.status_code == 200
    assert "12 mm" in about.text
    assert "about 10 mm" not in about.text
    plan = client.get("/forecast/plan", params=params)
    assert plan.status_code == 200
    assert "08:00–20:00" in plan.text
    assert "no weather station attached" in plan.text
    assert "No treatment clearance" in plan.text


@pytest.mark.parametrize("scope_change", ["year", "region"])
def test_scope_change_prevents_activation_and_parent_inheritance(
    client, session, settings, scope_change
):
    from datetime import UTC, datetime

    from farmcore.core.clock import set_replay

    farm = Farm(name="Scope-changed farm", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    params = {"farm": farm.id}
    body = {"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "Scope trial"}
    result = client.post("/review/versions", params=params, json=body)
    assert result.status_code == 201
    version = result.json()["id"]
    assert (
        client.post(
            f"/review/versions/{version}/reviews",
            params=params,
            json={"verdict": "approved", "note": "Opinion", "evidence": "Desk"},
        ).status_code
        == 201
    )
    if scope_change == "year":
        set_replay(session, datetime(2025, 4, 9, tzinfo=UTC), settings)
    else:
        session.get(Farm, farm.id).props = {"region": "ES418"}
    session.commit()
    assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 409
    child = client.post(
        "/review/versions", params=params, json={**body, "parent_version_id": version}
    )
    assert child.status_code == 422
    assert child.json()["detail"] == "Parent version is unavailable for this scope"
    for opinion in (
        {"verdict": "approved", "note": " ", "evidence": "Desk"},
        {"verdict": "approved", "note": "Opinion", "evidence": " "},
    ):
        assert (
            client.post(
                f"/review/versions/{version}/reviews", params=params, json=opinion
            ).status_code
            == 422
        )
