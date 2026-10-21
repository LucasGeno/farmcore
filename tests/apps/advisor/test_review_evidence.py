"""Synthetic job and HTTP missingness/provenance, independent of demo datasets."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.apps.advisor.job import evaluate
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Advice, Asset, Farm, Parcel
from tests.apps.chat.test_handlers import seed_farm

NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)


def test_empty_station_job_is_unavailable_through_persisted_http(client, session, settings):
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    farm = session.get(Farm, parcel.farm_id)
    session.add(Asset(farm_id=farm.id, kind="station", name="Empty test station", props={}))
    session.flush()
    evaluate(JobContext(session, NOW, settings, (farm.id,)))
    row = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )
    response = client.get(f"/advisor/parcels/{parcel.id}.json")
    assert response.status_code == 200
    body = response.json()
    assert body["assessment_status"] == "unavailable"
    assert body["headline"] == "Assessment unavailable: missing hourly observations"
    assert body["config_version"]["status"] == "unreviewed"
    assert row.props["effective_config"]["downy_rain_min_mm"]["value"] == 10.0
    assert row.props["input_snapshot"]["observations"] == []
    assert row.props["input_snapshot"]["evaluation_at"] == NOW.isoformat()
    assert row.props["input_snapshot"]["observation_receipt"] == "unknown"
    page = client.get(f"/advisor/parcels/{parcel.id}").text
    assert "Assessment unavailable" in page
    assert "Nothing to do today" not in page


def test_legacy_assessment_with_empty_attached_station_is_currently_unknown(
    client, session, settings
):
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    session.add(
        Asset(farm_id=parcel.farm_id, kind="station", name="Empty legacy station", props={})
    )
    row = Advice(
        farm_id=parcel.farm_id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="none",
        reasons=[
            {
                "rule": "downy_3_10",
                "inputs": {},
                "threshold": {},
                "verdict": "no risk",
                "source": "Legacy",
            }
        ],
        products=[],
        props={"headline": "No action: no infection risk"},
    )
    session.add(row)
    session.flush()
    response = client.get(f"/advisor/parcels/{parcel.id}.json")
    assert response.status_code == 200
    body = response.json()
    assert body["assessment_status"] == "unavailable"
    assert body["config_version"]["status"] == "legacy-unversioned"
    assert "missing hourly observations" in body["headline"]
    assert row.reasons[0]["verdict"] == "no risk"
    assert row.props == {"headline": "No action: no infection risk"}


def test_job_snapshots_exact_inputs_and_keeps_rejected_raw_values(session, settings):
    from datetime import timedelta
    from decimal import Decimal

    from farmcore.core.models.canonical import Log, LogSource, Observation, Product, Quantity
    from tests.apps.advisor.review_fixtures import complete_station

    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    station = complete_station(session, parcel.farm_id, NOW)
    copper = session.scalar(select(Product).where(Product.active_substance == "copper"))
    log = Log(
        farm_id=parcel.farm_id,
        type="spray",
        status="done",
        name="Exact input",
        ts_start=NOW - timedelta(hours=1),
        parcels=[parcel],
    )
    session.add(log)
    session.flush()
    literal = "0.12345678901234567890123456789"
    quantities = [
        Quantity(
            log_id=log.id,
            product_id=copper.id,
            measure="rate",
            value=Decimal(literal),
            unit="kg/ha",
        ),
        Quantity(
            log_id=log.id,
            product_id=copper.id,
            measure="rate",
            value=Decimal("2.00000000000000000000000000001"),
            unit="kg/ha",
        ),
    ]
    sources = [
        LogSource(
            log_id=log.id,
            source_type="file",
            external_key="exact-source-a",
            raw_payload={"entered": literal},
            precision_rank=2,
            confidence=None,
        ),
        LogSource(
            log_id=log.id,
            source_type="voice",
            external_key="exact-source-b",
            raw_payload={"transcript": "exact confirmation"},
            precision_rank=1,
            confidence=0.8,
        ),
    ]
    session.add_all([*quantities, *sources])
    bad = session.scalar(
        select(Observation).where(
            Observation.asset_id == station.id, Observation.ts == NOW, Observation.variable == "t_c"
        )
    )
    bad.value = float("nan")
    session.flush()
    evaluate(JobContext(session, NOW, settings, (parcel.farm_id,)))
    row = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )
    snapshot = row.props["input_snapshot"]
    assert [q["quantity"]["id"] for q in snapshot["quantities"]] == [q.id for q in quantities]
    assert snapshot["quantities"][0]["quantity"]["value"] == literal
    assert snapshot["quantities"][1]["quantity"]["value"] == "2.00000000000000000000000000001"
    assert [s["id"] for s in snapshot["log_sources"]] == [s.id for s in sources]
    assert [s["external_key"] for s in snapshot["log_sources"]] == [
        "exact-source-a",
        "exact-source-b",
    ]
    assert snapshot["log_sources"][0]["raw_payload"] == {"entered": literal}
    assert [s["precision_rank"] for s in snapshot["log_sources"]] == [2, 1]
    assert row.props["assessment_status"] == "limited"
    observation = next(o for o in snapshot["observations"] if o["id"] == bad.id)
    assert observation["value"] == {"rejected": "non-finite", "raw": "nan"}
    hourly = next(o for o in snapshot["hourly_inputs"] if o["ts"] == NOW.isoformat())
    assert hourly["t_c"] == {"rejected": "non-finite", "raw": "nan"}


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
    assert body["input_snapshot"] == {"station": {"id": 100}}
    assert body["current_availability"] is None
    assert row.props["headline"] == "Original saved protection"


@pytest.mark.parametrize("bad_id", [0, 2**63, 2**63 - 1])
@pytest.mark.parametrize("part", ["parcel", "assessment"])
def test_exact_assessment_identifiers_are_bounded_and_never_leak(
    client, session, settings, bad_id, part
):
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    row = Advice(
        farm_id=parcel.farm_id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="none",
        reasons=[],
        products=[],
        props={"input_snapshot": {"private_marker": "secret-evidence"}},
    )
    session.add(row)
    session.flush()
    parcel_id = bad_id if part == "parcel" else parcel.id
    advice_id = bad_id if part == "assessment" else row.id
    response = client.get(
        f"/advisor/parcels/{parcel_id}/assessments/{advice_id}.json",
        params={"farm": parcel.farm_id},
    )
    assert response.status_code == 404
    assert "secret-evidence" not in response.text
    assert "parameter_" not in response.text


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


@pytest.mark.parametrize(
    "observed_alert,risk_age_hours,known_window",
    [(False, 24, False), (True, 24, False), (False, 12, False), (False, 24, True)],
)
def test_limited_incubation_priority_never_invents_stage_or_window_timing(
    client, session, settings, observed_alert, risk_age_hours, known_window
):
    from copy import deepcopy
    from datetime import timedelta

    from farmcore.core.models.canonical import Observation
    from tests.apps.advisor.review_fixtures import complete_station

    now = datetime(2024, 4, 17, 12, tzinfo=UTC)
    ids = seed_farm(session, settings, now)
    parcel = session.get(Parcel, ids["P1"])
    station = complete_station(
        session, parcel.farm_id, now + timedelta(hours=80) if known_window else now
    )
    if observed_alert:
        params = {"farm": parcel.farm_id}
        proposal = client.post(
            "/review/versions",
            params=params,
            json={"overrides": {"incub_alert_pct": 1.0}, "evidence": "Bounded priority trial"},
        )
        assert proposal.status_code == 201
        version = proposal.json()["id"]
        assert (
            client.post(
                f"/review/versions/{version}/reviews",
                params=params,
                json={"verdict": "approved", "note": "Opinion only", "evidence": "Desk"},
            ).status_code
            == 201
        )
        assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 201
    rain_row = session.scalar(
        select(Observation).where(
            Observation.asset_id == station.id,
            Observation.variable == "rr1_mm",
            Observation.ts == now - timedelta(hours=risk_age_hours),
        )
    )
    rain_row.value = 10
    missing_at = now - timedelta(hours=1)
    temperature = session.scalar(
        select(Observation).where(
            Observation.asset_id == station.id,
            Observation.variable == "t_c",
            Observation.ts == missing_at,
        )
    )
    session.delete(temperature)
    session.flush()
    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))
    row = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    )
    saved = deepcopy(row.props), deepcopy(row.reasons)
    body = client.get(f"/advisor/parcels/{parcel.id}.json").json()
    assert body["decision"] == ("spray" if known_window else "wait")
    assert body["assessment_status"] == "limited"
    assert (body["next_window"] is not None) is known_window
    assert body["priority_label"] == (
        "Urgent" if observed_alert else "High" if risk_age_hours >= 24 else "Due"
    )
    assert "Incubation timing is not evaluable" in body["priority_why"]
    assert (
        "a spray window is known, but its relation to incubation cannot be evaluated"
        if known_window
        else "no spray window is currently known"
    ) in body["priority_why"]
    assert "before the alert stage" not in body["priority_why"]
    assert "a spray window opens before it ends" not in body["priority_why"]
    incubation = next(r for r in body["reasons"] if r["rule"] == "downy_incubation")
    assert incubation["inputs"]["pct_basis"] == "observed-hours-only"
    assert incubation["inputs"]["expected_symptoms"] is None
    page = client.get("/advisor/").text
    assert "Incubation timing is not evaluable" in page
    assert "a spray window opens before it ends" not in page
    session.add(Observation(asset_id=station.id, ts=missing_at, variable="t_c", value=15))
    session.flush()
    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))
    complete = client.get(f"/advisor/parcels/{parcel.id}.json").json()
    assert complete["priority_label"] == ("High" if known_window else "Urgent")
    assert (complete["next_window"] is not None) is known_window
    if not known_window:
        assert "no spray window before incubation ends" in complete["priority_why"]
    session.refresh(row)
    assert (row.props, row.reasons) == saved


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
        assert body["facts"]["parcels"][0]["config_version"]["status"] == "unsure"
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
    ],
)
def test_consumers_show_saved_advice_as_unavailable_without_station_readings(
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
    response = client.get(path)
    assert response.status_code == 200
    if path.endswith(".geojson"):
        props = next(
            f["properties"]
            for f in response.json()["features"]
            if f["properties"]["id"] == parcel.id
        )
        expected = (
            {"label": "Assessment unavailable", "protected_until": None}
            if path.startswith("/advisor")
            else {"assessment_status": "unavailable", "advised": True, "decision": "none"}
        )
        assert {key: props[key] for key in expected} == expected
        return
    assert "unavailable" in response.text.lower()
    assert "Protected until tomorrow" not in response.text
    assert ">Protected<" not in response.text
    assert not any(claim in response.text for claim in REASSURANCE)
