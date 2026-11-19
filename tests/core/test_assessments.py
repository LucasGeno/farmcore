"""The assessment evidence envelope: typed results per kind, immutable evidence, farm isolation,
and an upgrade that leaves legacy crop-protection advice exactly as it was."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from farmcore.apps.advisor.advice import Advice
from farmcore.apps.advisor.assessment import KIND as SPRAY
from farmcore.core import assessments
from farmcore.core.commands.migrate import alembic_config
from farmcore.core.manifest import AssessmentKind, Manifest, collect_assessment_kinds
from farmcore.core.models.assessment import Assessment
from farmcore.core.models.canonical import Farm
from tests.apps.operations.conftest import make_parcel

NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)
SPRAY_RESULT = {
    "decision": "wait",
    "headline": "Wait: infection risk after 11.0 mm rain, not protected; next window today",
    "reasons": [
        {
            "rule": "downy_3_10",
            "inputs": {"event_r48_mm": 11.0},
            "threshold": {"rain_mm": 10},
            "verdict": "infection risk",
            "source": "3-10 rule",
            "note": "",
        }
    ],
    "products": [{"name": "Copper", "product_type": "contact", "targets": ["downy"]}],
    "next_window": ["2024-04-10T06:00:00+00:00", "2024-04-10T10:00:00+00:00"],
    "protected_until": None,
    "flags": [],
    "mm_since_spray": None,
    "incubation_pct": None,
    "passes": 1,
    "treatments": 1,
}


class FrostHours(BaseModel):
    """A differently typed result another module might keep: no decision, no products."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    hours_below: int
    threshold_c: Decimal


FROST = AssessmentKind("frost.hours", 1, f"{__name__}:FrostHours")


def _envelope(kind, *, farm_id, parcel_id=None, result, status="evaluable"):
    return assessments.envelope(
        kind,
        farm_id=farm_id,
        parcel_id=parcel_id,
        evaluated_at=NOW,
        status=status,
        config_version={"id": None, "status": "unreviewed", "accuracy_claim": False},
        input_snapshot={"hourly_inputs": []},
        result=result,
    )


def _farm(session, name):
    farm = Farm(name=name, props={})
    session.add(farm)
    session.flush()
    return farm


def test_envelope_keeps_a_second_kind_typed_and_never_as_a_spray_result(session):
    farm = _farm(session, "Two kinds")
    parcel = make_parcel(session, farm, 1, area="1", props={})
    frost = _envelope(FROST, farm_id=farm.id, result={"hours_below": 3, "threshold_c": "-1.5"})
    spray = _envelope(SPRAY, farm_id=farm.id, parcel_id=parcel.id, result=SPRAY_RESULT)
    session.add_all([frost, spray])
    session.flush()
    session.expire_all()

    assert assessments.result_of(session.get(Assessment, frost.id), FROST) == FrostHours(
        hours_below=3, threshold_c=Decimal("-1.5")
    )
    assert assessments.result_of(session.get(Assessment, spray.id), SPRAY).decision == "wait"
    # Advisor validation is not relaxed for the envelope's sake: no frost result passes as a
    # spray result, no unknown decision or field is stored, and kinds are never read crosswise.
    for wrong in (
        {"hours_below": 3, "threshold_c": "-1.5"},
        {**SPRAY_RESULT, "decision": "frost"},
        {**SPRAY_RESULT, "frost_hours": 3},
    ):
        with pytest.raises(assessments.AssessmentInvalid):
            _envelope(SPRAY, farm_id=farm.id, parcel_id=parcel.id, result=wrong)
    with pytest.raises(assessments.AssessmentInvalid):
        assessments.result_of(session.get(Assessment, frost.id), SPRAY)
    # A saved v1 result is never read as a later shape, even when it would happen to validate.
    with pytest.raises(assessments.AssessmentInvalid):
        assessments.result_of(
            session.get(Assessment, spray.id), AssessmentKind(SPRAY.key, 2, SPRAY.result)
        )


def test_two_apps_cannot_declare_the_same_assessment_kind():
    one = Manifest(name="one", version="1", summary="", assessment_kinds=(FROST,))
    two = Manifest(name="two", version="1", summary="", assessment_kinds=(FROST,))
    with pytest.raises(ValueError, match="frost.hours"):
        collect_assessment_kinds([one, two])


def test_saved_assessment_evidence_cannot_be_rewritten(session):
    farm = _farm(session, "Immutable evidence")
    parcel = make_parcel(session, farm, 1, area="1", props={})
    row = _envelope(SPRAY, farm_id=farm.id, parcel_id=parcel.id, result=SPRAY_RESULT)
    session.add(row)
    session.flush()
    with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
        session.execute(
            update(Assessment)
            .where(Assessment.id == row.id)
            .values(input_snapshot={"hourly_inputs": [{"rr1_mm": 0.0}]})
        )


@pytest.mark.parametrize("other", ["farm", "parcel"])
def test_advice_cannot_point_at_another_farms_or_parcels_assessment(session, other):
    farm = _farm(session, "Owner")
    parcel = make_parcel(session, farm, 1, area="1", props={})
    if other == "farm":
        stranger = _farm(session, "Stranger")
        target = make_parcel(session, stranger, 2, area="1", props={})
    else:
        stranger, target = farm, make_parcel(session, farm, 2, area="1", props={})
    foreign = _envelope(SPRAY, farm_id=stranger.id, parcel_id=target.id, result=SPRAY_RESULT)
    session.add(foreign)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Advice(
                farm_id=farm.id,
                parcel_id=parcel.id,
                evaluated_at=NOW,
                decision="wait",
                assessment_id=foreign.id,
            )
        )
        session.flush()


@pytest.fixture
def scratch_db_url(settings):
    admin_url = make_url(settings.test_database_url)
    name = "farmcore_test_assess_" + uuid.uuid4().hex[:8]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


LEGACY_PROPS = {
    "headline": "Protected until 12 Apr 06:00: 2.0 mm rain since spray",
    "assessment_status": "evaluable",
    "config_version": {"id": None, "status": "unreviewed", "accuracy_claim": False},
    "effective_config": {"downy_rain_min_mm": {"value": 10.0, "source": "3-10 rule"}},
    "input_snapshot": {"hourly_inputs": [{"ts": "2024-04-10T03:00:00+00:00", "rr1_mm": "2.0"}]},
    "flags": [],
}


def test_upgrade_keeps_legacy_advice_and_downgrade_keeps_envelope_evidence(scratch_db_url):
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "core_0011")
    engine = create_engine(scratch_db_url)
    advice_sql = "SELECT decision, reasons, products, protected_until, props FROM advice"
    try:
        with engine.begin() as db:
            farm_id = db.execute(text("INSERT INTO farm(name) VALUES ('A') RETURNING id")).scalar()
            parcel_id = db.execute(
                text(
                    "INSERT INTO parcel(farm_id,name,alias,area_ha,geom) VALUES (:farm,'P','p',1,"
                    "ST_Multi(ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))',4326))) RETURNING id"
                ),
                {"farm": farm_id},
            ).scalar()
            db.execute(
                text(
                    "INSERT INTO advice(farm_id,parcel_id,evaluated_at,decision,reasons,"
                    "protected_until,props) VALUES (:farm,:parcel,'2024-04-10T04:00Z','protected',"
                    "'[{\"rule\": \"protection_downy\"}]','2024-04-12T06:00Z',:props)"
                ),
                {"farm": farm_id, "parcel": parcel_id, "props": json.dumps(LEGACY_PROPS)},
            )
            legacy = db.execute(text(advice_sql)).one()

        command.upgrade(cfg, "head")
        with engine.begin() as db:
            assert db.execute(text(advice_sql)).one() == legacy
            assert db.execute(text("SELECT assessment_id FROM advice")).scalar() is None
            assert db.execute(text("SELECT count(*) FROM assessment")).scalar() == 0
            # A row written after the upgrade keeps its heavy evidence only on the envelope.
            envelope_id = db.execute(
                text(
                    "INSERT INTO assessment(farm_id,parcel_id,kind,kind_version,evaluated_at,"
                    "status,config_version,effective_config,input_snapshot,result) VALUES "
                    "(:farm,:parcel,'advisor.spray',1,'2024-04-11T04:00Z','evaluable',"
                    "'{\"id\": null}',:config,:snapshot,'{}') RETURNING id"
                ),
                {
                    "farm": farm_id,
                    "parcel": parcel_id,
                    "config": json.dumps(LEGACY_PROPS["effective_config"]),
                    "snapshot": json.dumps({"hourly_inputs": ["after upgrade"]}),
                },
            ).scalar()
            db.execute(
                text(
                    "INSERT INTO advice(farm_id,parcel_id,evaluated_at,decision,props,"
                    "assessment_id) VALUES (:farm,:parcel,'2024-04-11T04:00Z','none',"
                    '\'{"headline": "No action"}\',:envelope)'
                ),
                {"farm": farm_id, "parcel": parcel_id, "envelope": envelope_id},
            )

        command.downgrade(cfg, "core_0011")
        with engine.begin() as db:
            rows = db.execute(text(advice_sql + " ORDER BY evaluated_at")).all()
        assert rows[0] == legacy
        assert rows[1].props == {
            "headline": "No action",
            "config_version": {"id": None},
            "effective_config": LEGACY_PROPS["effective_config"],
            "input_snapshot": {"hourly_inputs": ["after upgrade"]},
        }
    finally:
        engine.dispose()
