"""Synthetic job and HTTP missingness/provenance, independent of demo datasets."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.advisor.job import evaluate
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Advice, Asset, Farm, Parcel
from tests.apps.chat.test_handlers import seed_farm

NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)


def test_exact_assessment_read_survives_station_detach_and_current_overlay(
    client, session, settings
):
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    row = Advice(
        farm_id=parcel.farm_id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="protected",
        reasons=[],
        products=[],
        props={
            "headline": "Original saved protection",
            "assessment_status": "evaluable",
            "config_version": {"id": 31, "status": "approved"},
            "effective_config": {"frozen": True},
            "input_snapshot": {"station": {"id": 100}},
        },
    )
    session.add(row)
    session.flush()
    current = client.get(f"/advisor/parcels/{parcel.id}.json")
    assert current.status_code == 200
    assert current.json()["assessment_status"] == "unavailable"
    assert current.json()["assessment_status_at_evaluation"] == "evaluable"
    assert current.json()["current_availability"]["status"] == "unavailable"
    historical = client.get(f"/advisor/parcels/{parcel.id}/assessments/{row.id}.json")
    assert historical.status_code == 200
    body = historical.json()
    assert body["decision"] == "protected"
    assert body["assessment_status"] == body["assessment_status_at_evaluation"] == "evaluable"
    assert body["headline"] == "Original saved protection"
    assert body["effective_config"] == {"frozen": True}
    assert body["config_verification"] == "saved copy"
    assert body["input_snapshot"] == {"station": {"id": 100}}
    assert body["current_availability"] is None
    assert row.props["headline"] == "Original saved protection"


def test_job_scope_uses_one_version_for_each_parcel_and_leaves_other_farm_unchanged(
    client, session, settings
):
    from copy import deepcopy

    from farmcore.core.access import ensure_owner_grant
    from tests.apps.advisor.review_fixtures import complete_station
    from tests.apps.operations.conftest import make_parcel

    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    complete_station(session, parcel.farm_id, NOW)
    foreign = Farm(name="Untouched evaluation farm", props={"region": "FRI12"})
    session.add(foreign)
    session.flush()
    ensure_owner_grant(session, foreign)
    foreign_parcel = make_parcel(session, foreign, 900, area="1", props={})
    original = Advice(
        farm_id=foreign.id,
        parcel_id=foreign_parcel.id,
        evaluated_at=NOW,
        decision="wait",
        reasons=[{"rule": "saved-foreign-evidence"}],
        products=[],
        props={"saved": "unchanged"},
    )
    session.add(original)
    session.flush()
    before = deepcopy(original.props), deepcopy(original.reasons)
    evaluate(JobContext(session, NOW, settings, (parcel.farm_id,)))
    rows = session.scalars(select(Advice).where(Advice.farm_id == parcel.farm_id)).all()
    assert len(rows) == 3
    assert len({row.props["config_version"]["id"] for row in rows}) == 1
    assert len({row.props["config_version"]["hash"] for row in rows}) == 1
    session.refresh(original)
    assert (original.props, original.reasons) == before
    assert session.scalars(select(Advice).where(Advice.farm_id == foreign.id)).all() == [original]


@pytest.mark.parametrize("state", ["no station", "limited"])
def test_assistant_never_says_nothing_to_spray_without_a_complete_assessment(
    client, session, settings, state
):
    from tests.apps.advisor.review_fixtures import complete_station

    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    props = {"headline": "No action: no infection risk"}
    if state == "limited":
        complete_station(session, parcel.farm_id, NOW)
        props = {
            "assessment_status": "limited",
            "headline": "Assessment limited: incomplete required inputs; infection risk is unknown",
            "config_version": {"id": 19, "hash": "a" * 64, "status": "unsure"},
            "input_snapshot": {"private_marker": "raw-inputs"},
        }
    session.add(
        Advice(
            farm_id=parcel.farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="none",
            reasons=[],
            products=[],
            props=props,
        )
    )
    session.flush()
    response = client.get("/assistant/ask", params={"q": "What should I spray today?"})
    assert response.status_code == 200
    body = response.json()
    assert "Nothing to spray" not in body["text"]
    assert "raw-inputs" not in response.text
    if state == "limited":
        assert "limited" in body["text"].lower()
        assert body["facts"]["parcels"][0]["assessment_status"] == "limited"
        # The model learns which version and its review status, never its hash or scope.
        assert body["facts"]["parcels"][0]["config_version"] == {"id": 19, "status": "unsure"}
    else:
        assert "unavailable" in body["text"].lower()
        assert body["facts"]["unavailable"].startswith("Assessment unavailable")


REASSURANCE = ("Nothing to spray today", "no infection risk right now", "No spray needed")


@pytest.mark.parametrize(
    "path",
    [
        "/advisor/today",
        "/advisor/today?view=calm",
        "/advisor/map/farm",
        "/advisor/layer.geojson",
        "/fields/parcels.geojson",
        "/fields/list",
        "/forecast/plan",
        "/advisor/parcels/{id}.json",
    ],
)
def test_consumers_show_saved_legacy_advice_as_unavailable_without_station_readings(
    client, session, settings, path
):
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    session.add(Asset(farm_id=parcel.farm_id, kind="station", name="No readings", props={}))
    session.add(
        Advice(
            farm_id=parcel.farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="protected",
            reasons=[],
            products=[],
            props={"headline": "Protected until tomorrow"},
        )
    )
    session.flush()
    response = client.get(path.format(id=parcel.id))
    assert response.status_code == 200
    if path.endswith("}.json"):
        card = response.json()
        assert card["assessment_status"] == "unavailable"
        assert card["assessment_status_at_evaluation"] == "legacy-unknown"
        assert card["config_version"]["status"] == "legacy-unversioned"
        assert "missing hourly observations" in card["headline"]
        return
    if path.endswith(".geojson"):
        props = next(
            f["properties"]
            for f in response.json()["features"]
            if f["properties"]["id"] == parcel.id
        )
        expected = (
            {"label": "Assessment unavailable", "colour": "#9ca3af", "protected_until": None}
            if path.startswith("/advisor")
            else {"assessment_status": "unavailable", "advised": True, "decision": "none"}
        )
        assert {key: props[key] for key in expected} == expected
        return
    assert "unavailable" in response.text.lower()
    assert "Protected until tomorrow" not in response.text
    assert ">Protected<" not in response.text
    assert not any(claim in response.text for claim in REASSURANCE)


def _evaluated(session, settings, parcel, station):
    evaluate(JobContext(session, NOW, settings, (parcel.farm_id,)))
    return session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )


def test_stored_series_version_and_sprays_reproduce_the_assessment(session, settings):
    from decimal import Decimal

    from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs, ProductRef, Spray
    from farmcore.apps.advisor.engine import evaluate as engine_evaluate
    from farmcore.apps.advisor.service import saved_config
    from farmcore.core.access import repo_for_job
    from farmcore.core.models.canonical import Log, Observation, Product, Quantity
    from tests.apps.advisor.review_fixtures import complete_station

    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    station = complete_station(session, parcel.farm_id, NOW)

    def reading(variable, ts):
        return session.scalar(
            select(Observation).where(
                Observation.asset_id == station.id,
                Observation.variable == variable,
                Observation.ts == ts,
            )
        )

    rainy = reading("rr1_mm", datetime(2024, 4, 8, 4, tzinfo=UTC))
    rainy.value = 11.0
    reading("t_c", datetime(2024, 3, 15, tzinfo=UTC)).value = float("nan")
    copper = session.scalar(select(Product).where(Product.active_substance == "copper"))
    log = Log(
        farm_id=parcel.farm_id,
        type="spray",
        status="done",
        name="Copper after the rain",
        ts_start=NOW - timedelta(hours=12),
        parcels=[parcel],
    )
    session.add(log)
    session.flush()
    session.add(
        Quantity(
            log_id=log.id, product_id=copper.id, measure="rate", value=Decimal("2"), unit="kg/ha"
        )
    )
    session.flush()
    row = _evaluated(session, settings, parcel, station)
    rainy.value = 0.0  # a later station correction must not change the saved evidence
    session.flush()

    snapshot = row.props["input_snapshot"]
    late = next(h for h in snapshot["hourly_inputs"] if h["ts"].startswith("2024-03-15T00"))
    assert late["t_c"] == {"rejected": "non-finite", "raw": "nan"}
    config, verification = saved_config(
        repo_for_job(session, parcel.farm_id, "advisor.evaluate"), row
    )
    assert verification == "verified"

    def value(v):
        return float(v["raw"]) if isinstance(v, dict) else v

    series = [
        HourlyObs(
            datetime.fromisoformat(h["ts"]),
            *(value(h[k]) for k in ("rr1_mm", "t_c", "rh_pct", "ff_kmh")),
        )
        for h in snapshot["hourly_inputs"]
    ]
    sprays = [
        Spray(
            datetime.fromisoformat(s["ts_start"]),
            tuple(
                ProductRef(
                    q["product"]["name"],
                    q["product"]["product_type"],
                    tuple(q["product"]["targets"]),
                )
                for q in snapshot["quantities"]
                if q["quantity"]["log_id"] == s["id"] and q["product"]
            ),
        )
        for s in snapshot["spray_logs"]
    ]
    again = engine_evaluate(series, sprays, AdvisorConfig.model_validate(config), NOW)
    assert row.decision == "protected"
    assert (again.decision, again.assessment_status) == (row.decision, "evaluable")
    assert [(r.rule, r.verdict) for r in again.reasons] == [
        (r["rule"], r["verdict"]) for r in row.reasons
    ]


def test_saved_config_is_the_rows_own_version_and_a_hash_mismatch_is_unverifiable(
    client, session, settings
):
    from tests.apps.advisor.review_fixtures import complete_station

    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    station = complete_station(session, parcel.farm_id, NOW)
    params = {"farm": parcel.farm_id}

    def activate(mm):
        version = client.post(
            "/review/versions",
            params=params,
            json={"overrides": {"downy_rain_min_mm": mm}, "evidence": "Trial"},
        ).json()["id"]
        client.post(
            f"/review/versions/{version}/reviews",
            params=params,
            json={"verdict": "approved", "note": "Opinion", "evidence": "Desk"},
        )
        assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 201

    activate(12.0)
    first = _evaluated(session, settings, parcel, station)
    activate(13.0)  # the latest version is no longer the one `first` ran with
    tampered = Advice(
        farm_id=parcel.farm_id,
        parcel_id=parcel.id,
        evaluated_at=NOW - timedelta(hours=1),
        decision="protected",
        reasons=[],
        products=[],
        props={
            **first.props,
            "config_version": {**first.props["config_version"], "hash": "0" * 64},
        },
    )
    session.add(tampered)
    session.flush()

    def saved(row):
        return client.get(
            f"/advisor/parcels/{parcel.id}/assessments/{row.id}.json", params=params
        ).json()

    body = saved(first)
    assert body["config_verification"] == "verified"
    assert body["effective_config"]["downy_rain_min_mm"]["value"] == 12.0
    body = saved(tampered)
    assert body["config_verification"] == "unverifiable"
    assert body["effective_config"] is None
    assert body["assessment_status"] == "limited"
