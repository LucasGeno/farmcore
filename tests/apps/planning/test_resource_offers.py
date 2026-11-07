"""Resource assessment and alternatives are visible, honest and side-effect free."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from farmcore.apps.forecast import sources, store
from farmcore.apps.forecast.models import ForecastAvailability
from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobAssignment,
    PlanningJobRevision,
    PlanningMachine,
    PlanningOperator,
    PlanningResourceWindow,
)
from tests.apps.planning.conftest import NOW, job_payload
from tests.apps.share.conftest import make_parcel

FIXTURE = Path(__file__).parents[1] / "forecast" / "fixtures" / "forecast-runs.json"


def _forecast(session, farm):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    session.flush()


def _machine(session, farm, name, *, work_rate=None, active=True):
    row = PlanningMachine(
        farm_id=farm.id,
        name=name,
        kind="sprayer",
        work_rate_ha_per_hour=work_rate,
        active=active,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def _operator(session, farm, name="Ana"):
    row = PlanningOperator(
        farm_id=farm.id,
        name=name,
        active=True,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def _window(
    session, farm, *, machine=None, operator=None, start_hour, end_hour, available=True, day=None
):
    base = NOW.replace(day=day) if day is not None else NOW
    row = PlanningResourceWindow(
        farm_id=farm.id,
        machine_id=machine.id if machine else None,
        operator_id=operator.id if operator else None,
        starts_at=base.replace(hour=start_hour, minute=0),
        ends_at=base.replace(hour=end_hour, minute=0),
        available=available,
        notes="",
        created_by="owner",
        updated_by="owner",
    )
    session.add(row)
    session.flush()
    return row


def _created(client, farm, parcels, products, advice, **overrides):
    response = client.post(
        "/planning/jobs",
        params={"farm": farm.id},
        json=job_payload(parcels, products, advice, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _assign(client, farm, job_id, kind, resource_id, revision=1):
    response = client.post(
        f"/planning/jobs/{job_id}/assignments",
        params={"farm": farm.id},
        json={"expected_revision": revision, "kind": kind, "resource_id": resource_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _detail(client, farm, job_id):
    response = client.get(f"/planning/jobs/{job_id}.json", params={"farm": farm.id})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_job_combines_weather_resources_and_known_capacity(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    machine = _machine(session, farm, "Sprayer 1", work_rate=2.0)
    operator = _operator(session, farm)
    _window(session, farm, machine=machine, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    body = _detail(client, farm, created["id"])
    assessment = body["resource_assessment"]
    assert assessment["status"] == "feasible", assessment
    assert assessment["weather"]["status"] == "open"
    assert assessment["machine"]["status"] == "available"
    assert assessment["operator"]["status"] == "available"
    assert assessment["capacity"]["status"] == "fits"
    assert assessment["reasons"] == []
    assert body["assignments"][0]["name"] == "Sprayer 1"

    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert "Feasible" in page.text
    assert "capacity unknown" not in page.text


def test_unknown_capacity_is_shown_and_never_called_feasible(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    machine = _machine(session, farm, "Hire sprayer", work_rate=None)
    operator = _operator(session, farm)
    _window(session, farm, machine=machine, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    assert assessment["status"] == "unknown"
    assert assessment["capacity"]["status"] == "unknown"
    assert "capacity" in " ".join(assessment["reasons"]).lower()
    assert assessment["offers"]["time"] is None

    page = client.get("/planning/", params={"farm": farm.id})
    assert "capacity unknown" in page.text.lower()


def test_an_infeasible_job_stays_visible_with_reason_and_offers_a_time(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    machine = _machine(session, farm, "Sprayer 1", work_rate=2.0)
    operator = _operator(session, farm)
    _window(session, farm, machine=machine, start_hour=4, end_hour=14)
    _window(session, farm, operator=operator, start_hour=10, end_hour=14)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    assert assessment["status"] == "infeasible"
    assert any("Operator" in reason for reason in assessment["reasons"])
    offer = assessment["offers"]["time"]
    assert offer is not None
    assert offer["start"].startswith("2024-04-09T10:00:00")
    assert offer["end"].startswith("2024-04-09T11:30:00")
    assert "side effects" not in offer["reason"].lower()

    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert "Infeasible" in page.text
    assert "outside configured availability" in page.text
    assert "Another eligible time" in page.text


def test_a_busy_machine_offers_another_configured_machine_but_not_unknown_capacity(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    busy = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    spare = _machine(session, farm, "Spare sprayer", work_rate=3.0)
    hire = _machine(session, farm, "Hire sprayer", work_rate=None)
    operator = _operator(session, farm)
    _window(session, farm, machine=busy, start_hour=5, end_hour=9, available=False)
    _window(session, farm, machine=spare, start_hour=5, end_hour=9)
    _window(session, farm, machine=hire, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", busy.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    assert assessment["status"] == "infeasible"
    by_name = {m["name"]: m for m in assessment["offers"]["machines"]}
    assert by_name["Spare sprayer"]["status"] == "available"
    assert by_name["Hire sprayer"]["status"] == "unknown"
    assert "capacity" in by_name["Hire sprayer"]["reason"].lower()


def test_an_infeasible_job_offers_another_parcel_with_current_advice(
    client, session, farm, parcels, products, advice
):
    from farmcore.core.models.canonical import Advice

    _forecast(session, farm)
    second = make_parcel(session, farm, 2)
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=second.id,
            evaluated_at=NOW,
            decision="spray",
            next_window_start=NOW + timedelta(hours=2),
            next_window_end=NOW + timedelta(hours=5),
            reasons=[],
            products=[],
            props={"assessment_status": "evaluable", "config_version": {"id": None, "status": "x"}},
        )
    )
    machine = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    _window(session, farm, machine=machine, start_hour=5, end_hour=9, available=False)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    by_alias = {p["alias"]: p for p in assessment["offers"]["parcels"]}
    assert by_alias["P2"]["status"] == "eligible"
    assert "1" not in by_alias


def test_a_busy_farm_day_keeps_the_job_infeasible_and_offers_another_day(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    machine = _machine(session, farm, "Sprayer 1", work_rate=2.0)
    operator = _operator(session, farm)
    for day in (9, 10):
        _window(session, farm, machine=machine, start_hour=5, end_hour=9, day=day)
        _window(session, farm, operator=operator, start_hour=5, end_hour=9, day=day)
    session.add(
        ForecastAvailability(
            farm_id=farm.id,
            day=NOW.date(),
            available=False,
            notes="Market day",
            created_by="owner",
            updated_by="owner",
        )
    )
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    assert assessment["status"] == "infeasible"
    assert any("Busy" in reason for reason in assessment["reasons"])
    offer = assessment["offers"]["time"]
    assert offer is not None
    assert offer["start"].startswith("2024-04-10T")


def test_wait_advice_and_a_non_overlapping_window_are_not_offered_as_eligible(
    client, session, farm, parcels, products, advice
):
    from farmcore.core.models.canonical import Advice

    _forecast(session, farm)
    waiting = make_parcel(session, farm, 2)
    elsewhere = make_parcel(session, farm, 3)
    session.add_all(
        [
            Advice(
                farm_id=farm.id,
                parcel_id=waiting.id,
                evaluated_at=NOW,
                decision="wait",
                next_window_start=NOW + timedelta(hours=2),
                next_window_end=NOW + timedelta(hours=5),
                reasons=[],
                products=[],
                props={
                    "assessment_status": "evaluable",
                    "config_version": {"id": None, "status": "x"},
                },
            ),
            Advice(
                farm_id=farm.id,
                parcel_id=elsewhere.id,
                evaluated_at=NOW,
                decision="spray",
                next_window_start=NOW + timedelta(hours=20),
                next_window_end=NOW + timedelta(hours=22),
                reasons=[],
                products=[],
                props={
                    "assessment_status": "evaluable",
                    "config_version": {"id": None, "status": "x"},
                },
            ),
        ]
    )
    machine = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    _window(session, farm, machine=machine, start_hour=5, end_hour=9, available=False)
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)

    offers = _detail(client, farm, created["id"])["resource_assessment"]["offers"]["parcels"]
    by_alias = {offer["alias"]: offer for offer in offers}
    assert by_alias["P2"]["status"] == "not_eligible"
    assert "wait" in by_alias["P2"]["reason"].lower()
    assert by_alias["P3"]["status"] == "not_eligible"
    assert "window" in by_alias["P3"]["reason"].lower()


def test_a_parcel_advice_window_must_cover_the_full_job_duration(
    client, session, farm, parcels, products, advice
):
    from farmcore.core.models.canonical import Advice

    _forecast(session, farm)
    partial = make_parcel(session, farm, 2)
    covering = make_parcel(session, farm, 3)
    session.add_all(
        [
            Advice(
                farm_id=farm.id,
                parcel_id=partial.id,
                evaluated_at=NOW,
                decision="spray",
                # The 90-minute job runs 06:00-07:30 UTC; this starts 30 minutes late.
                next_window_start=NOW + timedelta(hours=2, minutes=30),
                next_window_end=NOW + timedelta(hours=5),
                reasons=[],
                products=[],
                props={
                    "assessment_status": "evaluable",
                    "config_version": {"id": None, "status": "x"},
                },
            ),
            Advice(
                farm_id=farm.id,
                parcel_id=covering.id,
                evaluated_at=NOW,
                decision="spray",
                next_window_start=NOW + timedelta(hours=1),
                next_window_end=NOW + timedelta(hours=5),
                reasons=[],
                products=[],
                props={
                    "assessment_status": "evaluable",
                    "config_version": {"id": None, "status": "x"},
                },
            ),
        ]
    )
    machine = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    _window(session, farm, machine=machine, start_hour=5, end_hour=9, available=False)
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)

    offers = _detail(client, farm, created["id"])["resource_assessment"]["offers"]["parcels"]
    by_alias = {offer["alias"]: offer for offer in offers}
    assert by_alias["P2"]["status"] == "not_eligible"
    assert "cover" in by_alias["P2"]["reason"].lower()
    assert by_alias["P3"]["status"] == "eligible"


def test_an_alternate_machine_is_not_offered_on_a_busy_farm_day(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    busy = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    spare = _machine(session, farm, "Spare sprayer", work_rate=3.0)
    operator = _operator(session, farm)
    _window(session, farm, machine=busy, start_hour=5, end_hour=9, available=False)
    _window(session, farm, machine=spare, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9)
    session.add(
        ForecastAvailability(
            farm_id=farm.id,
            day=NOW.date(),
            available=False,
            notes="Market day",
            created_by="owner",
            updated_by="owner",
        )
    )
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", busy.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    by_name = {machine["name"]: machine for machine in assessment["offers"]["machines"]}
    assert by_name["Spare sprayer"]["status"] != "available"
    assert "busy" in by_name["Spare sprayer"]["reason"].lower()


def test_an_alternate_machine_also_needs_open_weather(
    client, session, farm, parcels, products, advice
):
    from sqlalchemy import update

    from farmcore.apps.forecast.models import ForecastValue

    _forecast(session, farm)
    # Rain from 06:00 UTC closes the 06:00-07:30 job window through the two-hour lookahead.
    session.execute(
        update(ForecastValue)
        .where(
            ForecastValue.variable == "rr1_mm",
            ForecastValue.ts >= NOW + timedelta(hours=2),
            ForecastValue.ts < NOW + timedelta(hours=6),
        )
        .values(value=10.0)
    )
    busy = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    spare = _machine(session, farm, "Spare sprayer", work_rate=3.0)
    operator = _operator(session, farm)
    _window(session, farm, machine=busy, start_hour=5, end_hour=9, available=False)
    _window(session, farm, machine=spare, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9)
    session.flush()
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", busy.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    by_name = {machine["name"]: machine for machine in assessment["offers"]["machines"]}
    assert by_name["Spare sprayer"]["status"] != "available"
    assert "weather" in by_name["Spare sprayer"]["reason"].lower()


def test_an_alternate_machine_also_needs_the_booked_operator(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    busy = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    spare = _machine(session, farm, "Spare sprayer", work_rate=3.0)
    operator = _operator(session, farm)
    _window(session, farm, machine=busy, start_hour=5, end_hour=9, available=False)
    _window(session, farm, machine=spare, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9, available=False)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", busy.id, 1)
    _assign(client, farm, created["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, created["id"])["resource_assessment"]
    by_name = {machine["name"]: machine for machine in assessment["offers"]["machines"]}
    assert by_name["Spare sprayer"]["status"] != "available"
    assert "operator" in by_name["Spare sprayer"]["reason"].lower()


def test_an_infeasible_job_offers_another_planned_job_that_can_run_now(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    busy = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    spare = _machine(session, farm, "Spare sprayer", work_rate=3.0)
    operator = _operator(session, farm)
    _window(session, farm, machine=busy, start_hour=5, end_hour=9, available=False)
    _window(session, farm, machine=spare, start_hour=5, end_hour=9)
    _window(session, farm, operator=operator, start_hour=5, end_hour=9)
    blocked = _created(client, farm, parcels, products, advice)
    ready = _created(client, farm, parcels, products, advice)
    _assign(client, farm, blocked["id"], "machine", busy.id, 1)
    _assign(client, farm, ready["id"], "machine", spare.id, 1)
    _assign(client, farm, ready["id"], "operator", operator.id, 2)

    assessment = _detail(client, farm, blocked["id"])["resource_assessment"]
    assert assessment["status"] == "infeasible"
    offered = {job["id"] for job in assessment["offers"]["jobs"]}
    assert ready["id"] in offered
    assert blocked["id"] not in offered


def test_reading_offers_writes_nothing_and_makes_no_unsupported_claims(
    client, session, farm, parcels, products, advice
):
    _forecast(session, farm)
    machine = _machine(session, farm, "Busy sprayer", work_rate=1.0)
    _window(session, farm, machine=machine, start_hour=5, end_hour=9, available=False)
    created = _created(client, farm, parcels, products, advice)
    _assign(client, farm, created["id"], "machine", machine.id, 1)

    def counts():
        return (
            session.scalar(select(func.count()).select_from(PlanningJobAssignment)),
            session.scalar(select(func.count()).select_from(PlanningJobRevision)),
            session.scalar(select(func.count()).select_from(PlanningJob)),
        )

    before = counts()
    first = _detail(client, farm, created["id"])
    again = _detail(client, farm, created["id"])
    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    assert counts() == before

    payload = json.dumps({"first": first, "again": again}).lower()
    for unsupported in ("probability", "soil", "dose", "cost", "price", "saving", "€"):
        assert unsupported not in payload
