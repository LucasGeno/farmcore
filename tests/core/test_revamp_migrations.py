"""Public migration checks: retained receipts, scope constraints and immutable history."""

import json
import uuid

import pytest
from alembic import command
from hypothesis import HealthCheck, example, given
from hypothesis import settings as property_settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, IntegrityError

from farmcore.core.commands.migrate import alembic_config


@pytest.fixture
def scratch_db_url(settings):
    admin_url = make_url(settings.test_database_url)
    name = "farmcore_test_revamp_" + uuid.uuid4().hex[:8]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def database(scratch_db_url):
    command.upgrade(alembic_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def farm(db, name="A"):
    return db.execute(
        text("INSERT INTO farm(name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()


def version(db, farm_id, *, parent=None, region="FRI12", baseline=False):
    return db.execute(
        text(
            "INSERT INTO parameter_version "
            "(farm_id,region,evaluation_year,parent_version_id,created_by,evidence,overrides,"
            "parameter_catalog,effective_config,config_hash,baseline) "
            "VALUES (:farm,:region,2024,:parent,'user:1','synthetic evidence',"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,:hash,:baseline) RETURNING id"
        ),
        {
            "farm": farm_id,
            "region": region,
            "parent": parent,
            "hash": "a" * 64,
            "baseline": baseline,
        },
    ).scalar_one()


def review(db, farm_id, version_id):
    return db.execute(
        text(
            "INSERT INTO parameter_review "
            "(farm_id,crop,region,evaluation_year,version_id,reviewer,verdict,note,evidence) "
            "VALUES (:farm,'vine','FRI12',2024,:version,'user:1','approved','opinion','evidence') "
            "RETURNING id"
        ),
        {"farm": farm_id, "version": version_id},
    ).scalar_one()


def activate(db, farm_id, version_id, review_id):
    return db.execute(
        text(
            "INSERT INTO parameter_activation "
            "(farm_id,crop,region,evaluation_year,version_id,review_id,actor) "
            "VALUES (:farm,'vine','FRI12',2024,:version,:review,'user:1') RETURNING id"
        ),
        {"farm": farm_id, "version": version_id, "review": review_id},
    ).scalar_one()


def test_parameter_history_survives_failed_update_and_delete(database):
    db = database
    f = farm(db)
    v = version(db, f)
    r = review(db, f, v)
    a = activate(db, f, v, r)
    for table, row_id in (
        ("parameter_version", v),
        ("parameter_review", r),
        ("parameter_activation", a),
    ):
        statement = text(f"SELECT to_jsonb(t) FROM {table} t WHERE id=:id")
        before = db.execute(statement, {"id": row_id}).scalar_one()
        for mutation in (
            f"UPDATE {table} SET region=region WHERE id=:id",
            f"DELETE FROM {table} WHERE id=:id",
        ):
            with pytest.raises(IntegrityError, match="append-only"), db.begin_nested():
                db.execute(text(mutation), {"id": row_id})
            assert db.execute(statement, {"id": row_id}).scalar_one() == before


def test_parameter_ancestry_reviews_and_activation_cannot_cross_scope(database):
    db = database
    first, second = farm(db), farm(db, "B")
    v = version(db, first)
    for foreign_farm, region in ((second, "FRI12"), (first, "OTHER")):
        with pytest.raises(IntegrityError), db.begin_nested():
            version(db, foreign_farm, parent=v, region=region)
    with pytest.raises(IntegrityError), db.begin_nested():
        review(db, second, v)
    approved = review(db, first, v)
    other_version = version(db, first)
    with pytest.raises(IntegrityError), db.begin_nested():
        activate(db, first, other_version, approved)
    with pytest.raises(IntegrityError), db.begin_nested():
        activate(db, second, v, approved)
    assert activate(db, first, v, approved) > 0


def test_only_one_baseline_per_scope_without_restricting_proposed_versions(database):
    f = farm(database)
    version(database, f, baseline=True)
    with pytest.raises(IntegrityError), database.begin_nested():
        version(database, f, baseline=True)
    assert version(database, f) != version(database, f)
    assert version(database, f, baseline=True, region="OTHER") > 0


def correction(db, farm_id, log_id, identity, *, actor="identity:1", key="request-1", live=True):
    snapshot = {"original_log_id": log_id, "weather": {"receipt": "immutable"}, "dose": "2.00"}
    row_id = db.execute(
        text(
            "INSERT INTO operation_correction "
            "(farm_id,original_log_id,live_log_id,creator_identity_id,request_key,actor_key,"
            "actor_label,reason,base_fingerprint,before_snapshot,after_snapshot) VALUES "
            "(:farm,:log,:live,:identity,:key,:actor,'Original actor','Correct dose',:hash,"
            "CAST(:snapshot AS jsonb),CAST(:snapshot AS jsonb)) RETURNING id"
        ),
        {
            "farm": farm_id,
            "log": log_id,
            "identity": identity,
            "live": log_id if live else None,
            "key": key,
            "actor": actor,
            "hash": "b" * 64,
            "snapshot": json.dumps(snapshot),
        },
    ).scalar_one()
    return row_id, snapshot


def test_correction_retains_evidence_after_live_log_and_identity_are_deleted(database):
    db = database
    f = farm(db)
    log = db.execute(
        text(
            "INSERT INTO log(farm_id,type,status,name,ts_start) "
            "VALUES (:farm,'spray','done','Original spray',now()) RETURNING id"
        ),
        {"farm": f},
    ).scalar_one()
    identity = db.execute(
        text(
            "INSERT INTO local_identity(login,password_hash) "
            "VALUES ('synthetic','not-a-login-credential') RETURNING id"
        )
    ).scalar_one()
    c, snapshot = correction(db, f, log, identity)
    db.execute(text("DELETE FROM log WHERE id=:id"), {"id": log})
    db.execute(text("DELETE FROM local_identity WHERE id=:id"), {"id": identity})
    row = db.execute(
        text(
            "SELECT original_log_id,live_log_id,creator_identity_id,actor_label,"
            "before_snapshot,after_snapshot FROM operation_correction WHERE id=:id"
        ),
        {"id": c},
    ).one()
    assert tuple(row) == (log, None, None, "Original actor", snapshot, snapshot)
    with pytest.raises(IntegrityError), db.begin_nested():
        correction(db, f, log, None, live=False)


def test_correction_retry_scope_and_terminal_metadata_are_atomic(database):
    db = database
    f = farm(db)
    log = db.execute(
        text(
            "INSERT INTO log(farm_id,type,status,name,ts_start) "
            "VALUES (:farm,'spray','done','Spray',now()) RETURNING id"
        ),
        {"farm": f},
    ).scalar_one()
    c, _ = correction(db, f, log, None)
    with pytest.raises(IntegrityError), db.begin_nested():
        correction(db, f, log, None)
    correction(db, f, log, None, actor="identity:2")
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text("UPDATE operation_correction SET status='confirmed' WHERE id=:id"), {"id": c}
        )
    assert (
        db.execute(
            text("SELECT status FROM operation_correction WHERE id=:id"), {"id": c}
        ).scalar_one()
        == "pending"
    )
    db.execute(
        text(
            "UPDATE operation_correction SET status='confirmed',finalized_at=now(),"
            "finalized_actor_key=actor_key,finalized_actor_label=actor_label WHERE id=:id"
        ),
        {"id": c},
    )
    assert (
        db.execute(
            text("SELECT count(*) FROM operation_correction WHERE farm_id=:id"), {"id": f}
        ).scalar_one()
        == 2
    )


def test_new_history_upgrade_and_round_trip_preserve_existing_records(scratch_db_url):
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "core_0003")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            f = farm(db)
            log = db.execute(
                text(
                    "INSERT INTO log(farm_id,type,status,name,ts_start,props) "
                    "VALUES (:farm,'spray','done','Original',now(),CAST(:props AS jsonb)) "
                    "RETURNING id"
                ),
                {"farm": f, "props": json.dumps({"kept": True})},
            ).scalar_one()
            db.execute(
                text(
                    "INSERT INTO data_grant(farm_id,grantee,scopes,purpose,valid_from) "
                    "VALUES (:farm,'Legacy name',ARRAY['*']::text[],'legacy',now())"
                ),
                {"farm": f},
            )
            original = db.execute(
                text("SELECT to_jsonb(t) FROM log t WHERE id=:id"), {"id": log}
            ).scalar_one()
        for target in ("head", "core_0004_grant_recipients", "head"):
            (command.downgrade if target == "core_0004_grant_recipients" else command.upgrade)(
                cfg, target
            )
            with engine.connect() as db:
                assert (
                    db.execute(
                        text("SELECT to_jsonb(t) FROM log t WHERE id=:id"), {"id": log}
                    ).scalar_one()
                    == original
                )
                assert db.execute(
                    text("SELECT grantee,recipient_identity_id FROM data_grant")
                ).all() == [("Legacy name", None)]
                assert db.execute(text("SELECT count(*) FROM local_identity")).scalar_one() == 0
                if target == "head":
                    for table in (
                        "operation_correction",
                        "parameter_version",
                        "parameter_review",
                        "parameter_activation",
                    ):
                        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    finally:
        engine.dispose()


# Each generated example uses its own farm/log; the fixture is a disposable transaction.
@property_settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@example(key="")
@example(key="   ")
@example(key="a" * 128)
@example(key="a" * 129)
@given(key=st.text(alphabet="abAB01 _-", max_size=130))
def test_generated_retry_keys_keep_principal_scope_and_reject_invalid_values(database, key):
    db = database
    f = farm(db)
    log = db.execute(
        text(
            "INSERT INTO log(farm_id,type,status,name,ts_start) "
            "VALUES (:farm,'spray','done','Generated',now()) RETURNING id"
        ),
        {"farm": f},
    ).scalar_one()
    if not key.strip(" ") or len(key) > 128:
        with pytest.raises((IntegrityError, DataError)), db.begin_nested():
            correction(db, f, log, None, key=key)
        assert (
            db.execute(
                text("SELECT count(*) FROM operation_correction WHERE farm_id=:farm"), {"farm": f}
            ).scalar_one()
            == 0
        )
    else:
        correction(db, f, log, None, key=key)
        with pytest.raises(IntegrityError), db.begin_nested():
            correction(db, f, log, None, key=key)
        correction(db, f, log, None, actor="identity:2", key=key)
        assert (
            db.execute(
                text("SELECT count(*) FROM operation_correction WHERE farm_id=:farm"), {"farm": f}
            ).scalar_one()
            == 2
        )


def planned_job(db, farm_id, assessment_id, product_id, *, version_id=None):
    return db.execute(
        text(
            "INSERT INTO planning_job (farm_id,name,assessment_id,config_version_id,"
            "config_version_status,product_id,scheduled_start,duration_minutes,created_by,"
            "updated_by) VALUES (:farm,'Spray block A',:advice,:version,'approved',:product,"
            "now(),90,'user:1','user:1') RETURNING id"
        ),
        {"farm": farm_id, "advice": assessment_id, "version": version_id, "product": product_id},
    ).scalar_one()


def test_planned_jobs_are_farm_scoped_with_append_only_revisions(database):
    db = database
    first, second = farm(db), farm(db, "B")
    parcel = db.execute(
        text(
            "INSERT INTO parcel(farm_id,name,alias,area_ha,geom) VALUES (:farm,'P','p',1,"
            "ST_Multi(ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))',4326))) RETURNING id"
        ),
        {"farm": first},
    ).scalar_one()
    advice = db.execute(
        text(
            "INSERT INTO advice(farm_id,parcel_id,evaluated_at,decision) "
            "VALUES (:farm,:parcel,now(),'spray') RETURNING id"
        ),
        {"farm": first, "parcel": parcel},
    ).scalar_one()
    product = db.execute(
        text(
            "INSERT INTO product(name,authorisation_no,active_substance,product_type) "
            "VALUES ('Copper','1','copper','contact') RETURNING id"
        )
    ).scalar_one()
    foreign_version = version(db, second)
    for kwargs in ({"farm_id": second}, {"version_id": foreign_version}):
        args = {"farm_id": first, "assessment_id": advice, "product_id": product} | kwargs
        with pytest.raises(IntegrityError), db.begin_nested():
            planned_job(db, args.pop("farm_id"), **args)
    job = planned_job(db, first, advice, product, version_id=version(db, first))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text("INSERT INTO planning_job_parcel VALUES (:farm,:job,:parcel)"),
            {"farm": second, "job": job, "parcel": parcel},
        )
    with pytest.raises(IntegrityError, match="cancel_metadata"), db.begin_nested():
        db.execute(text("UPDATE planning_job SET status='cancelled' WHERE id=:id"), {"id": job})
    revision = db.execute(
        text(
            "INSERT INTO planning_job_revision(farm_id,job_id,revision,action,status,snapshot,"
            "actor) VALUES (:farm,:job,1,'created','planned','{}'::jsonb,'user:1') RETURNING id"
        ),
        {"farm": first, "job": job},
    ).scalar_one()
    with pytest.raises(IntegrityError, match="append-only"), db.begin_nested():
        db.execute(
            text("UPDATE planning_job_revision SET reason='x' WHERE id=:id"), {"id": revision}
        )
    db.execute(text("DELETE FROM planning_job_revision WHERE id=:id"), {"id": revision})
    db.execute(text("DELETE FROM planning_job WHERE id=:id"), {"id": job})


def test_availability_is_one_row_per_farm_day(database):
    db = database
    f = farm(database)
    statement = text(
        "INSERT INTO forecast_availability(farm_id,day,available,created_by,updated_by) "
        "VALUES (:farm,'2026-11-07',false,'user:1','user:1')"
    )
    db.execute(statement, {"farm": f})
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(statement, {"farm": f})


def _planning_fixture(db):
    """Two farms; a parcel, advice and product on the first. Returns (first, second, args)."""
    first, second = farm(db), farm(db, "B")
    parcel = db.execute(
        text(
            "INSERT INTO parcel(farm_id,name,alias,area_ha,geom) VALUES (:farm,'P','p',1,"
            "ST_Multi(ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))',4326))) RETURNING id"
        ),
        {"farm": first},
    ).scalar_one()
    advice = db.execute(
        text(
            "INSERT INTO advice(farm_id,parcel_id,evaluated_at,decision) "
            "VALUES (:farm,:parcel,now(),'spray') RETURNING id"
        ),
        {"farm": first, "parcel": parcel},
    ).scalar_one()
    product = db.execute(
        text(
            "INSERT INTO product(name,authorisation_no,active_substance,product_type) "
            "VALUES ('Copper','1','copper','contact') RETURNING id"
        )
    ).scalar_one()
    return first, second, {"assessment_id": advice, "product_id": product}


def resource(db, kind, farm_id, name="R"):
    return db.execute(
        text(
            f"INSERT INTO planning_{kind}(farm_id,name,created_by,updated_by) "
            "VALUES (:farm,:name,'user:1','user:1') RETURNING id"
        ),
        {"farm": farm_id, "name": name},
    ).scalar_one()


def assign(db, farm_id, job, kind, resource_id, starts, ends):
    return db.execute(
        text(
            f"INSERT INTO planning_job_assignment(farm_id,job_id,{kind}_id,starts_at,ends_at,"
            "created_by,updated_by) VALUES (:farm,:job,:resource,CAST(:starts AS timestamptz),"
            "CAST(:ends AS timestamptz),'user:1','user:1') RETURNING id"
        ),
        {"farm": farm_id, "job": job, "resource": resource_id, "starts": starts, "ends": ends},
    ).scalar_one()


@pytest.mark.parametrize("kind", ["machine", "operator"])
def test_resource_bookings_are_farm_scoped_and_reject_overlap(database, kind):
    db = database
    first, second, args = _planning_fixture(db)
    jobs = [planned_job(db, first, **args) for _ in range(3)]
    mine, foreign = resource(db, kind, first), resource(db, kind, second)
    assign(db, first, jobs[0], kind, mine, "2026-11-07 08:00Z", "2026-11-07 10:00Z")
    # Overlap with the same resource: exclusion_violation (23P01), nothing written.
    overlap = f"ex_planning_job_assignment_{kind}"
    with pytest.raises(IntegrityError, match=overlap), db.begin_nested():
        assign(db, first, jobs[1], kind, mine, "2026-11-07 09:59Z", "2026-11-07 11:00Z")
    # Half-open: back-to-back bookings are allowed.
    assign(db, first, jobs[1], kind, mine, "2026-11-07 10:00Z", "2026-11-07 11:00Z")
    # Another farm's resource cannot be booked or given a window, even by guessed id.
    with pytest.raises(IntegrityError), db.begin_nested():
        assign(db, first, jobs[2], kind, foreign, "2026-11-08 08:00Z", "2026-11-08 09:00Z")
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                f"INSERT INTO planning_resource_window(farm_id,{kind}_id,starts_at,ends_at,"
                "available,created_by,updated_by) VALUES (:farm,:r,now(),now()+interval '1h',"
                "true,'user:1','user:1')"
            ),
            {"farm": first, "r": foreign},
        )
    # One resource of each kind per job; exactly one resource per row; non-empty ranges.
    other = resource(db, kind, first, "Other")
    one_per_job = f"ix_planning_job_assignment_job_{kind}"
    with pytest.raises(IntegrityError, match=one_per_job), db.begin_nested():
        assign(db, first, jobs[0], kind, other, "2026-11-09 08:00Z", "2026-11-09 09:00Z")
    with pytest.raises(IntegrityError, match="range"), db.begin_nested():
        assign(db, first, jobs[2], kind, other, "2026-11-09 08:00Z", "2026-11-09 08:00Z")
    with pytest.raises(IntegrityError, match="resource"), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO planning_job_assignment(farm_id,job_id,starts_at,ends_at,"
                "created_by,updated_by) VALUES (:farm,:job,now(),now()+interval '1h',"
                "'user:1','user:1')"
            ),
            {"farm": first, "job": jobs[2]},
        )
    # A booked resource cannot be deleted; deleting the job frees it.
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text(f"DELETE FROM planning_{kind} WHERE id=:id"), {"id": mine})
    for job in jobs[:2]:
        db.execute(text("DELETE FROM planning_job WHERE id=:id"), {"id": job})
    assert db.execute(text("SELECT count(*) FROM planning_job_assignment")).scalar_one() == 0
    db.execute(text(f"DELETE FROM planning_{kind} WHERE id=:id"), {"id": mine})


def test_unknown_capacity_stays_null_and_windows_cascade_with_their_machine(database):
    db = database
    f = farm(db)
    machine = resource(db, "machine", f)
    assert db.execute(
        text("SELECT work_rate_ha_per_hour, tank_litres FROM planning_machine WHERE id=:id"),
        {"id": machine},
    ).one() == (None, None)
    with pytest.raises(IntegrityError, match="work_rate"), db.begin_nested():
        db.execute(
            text("UPDATE planning_machine SET work_rate_ha_per_hour=0 WHERE id=:id"),
            {"id": machine},
        )
    db.execute(
        text(
            "INSERT INTO planning_resource_window(farm_id,machine_id,starts_at,ends_at,available,"
            "created_by,updated_by) VALUES (:farm,:m,now(),now()+interval '1h',false,"
            "'user:1','user:1')"
        ),
        {"farm": f, "m": machine},
    )
    db.execute(text("DELETE FROM planning_machine WHERE id=:id"), {"id": machine})
    assert db.execute(text("SELECT count(*) FROM planning_resource_window")).scalar_one() == 0


def test_resource_scheduling_downgrade_and_upgrade_round_trip(scratch_db_url):
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "planning_0001")
    engine = create_engine(scratch_db_url)
    try:
        with engine.connect() as db:
            assert (
                db.execute(text("SELECT to_regclass('planning_job_assignment')")).scalar() is None
            )
            assert db.execute(text("SELECT to_regclass('planning_job')")).scalar() is not None
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()


def spray_log(db, farm_id):
    return db.execute(
        text(
            "INSERT INTO log(farm_id,type,status,name,ts_start) "
            "VALUES (:farm,'spray','done','Completed spray',now()) RETURNING id"
        ),
        {"farm": farm_id},
    ).scalar_one()


COMPLETE = (
    "UPDATE planning_job SET status='completed',completed_by=:by,completed_at=now(),"
    "operation_log_id=:log,operation_request_key=:key WHERE id=:id"
)


def test_completed_jobs_link_one_same_farm_operation_with_consistent_metadata(database):
    db = database
    first, second, args = _planning_fixture(db)
    job, other = planned_job(db, first, **args), planned_job(db, first, **args)
    mine, foreign = spray_log(db, first), spray_log(db, second)
    complete = {"id": job, "by": "user:1", "log": mine, "key": "k1"}
    # Completion metadata is all-or-nothing and only on completed jobs; actors are non-blank.
    for change in (
        "status='completed'",
        "completed_by='user:1'",
        "operation_request_key='k1'",
        f"operation_log_id={mine}",
    ):
        with pytest.raises(IntegrityError, match="complete_metadata"), db.begin_nested():
            db.execute(text(f"UPDATE planning_job SET {change} WHERE id=:id"), {"id": job})
    for name, values in (("completed_by", {"by": " "}), ("operation_request_key", {"key": " "})):
        with pytest.raises(IntegrityError, match=name), db.begin_nested():
            db.execute(text(COMPLETE), complete | values)
    with pytest.raises(IntegrityError, match="cancelled_by"), db.begin_nested():
        db.execute(
            text(
                "UPDATE planning_job SET status='cancelled',cancelled_by=' ',cancelled_at=now() "
                "WHERE id=:id"
            ),
            {"id": job},
        )
    # Another farm's operation cannot be linked, even by guessed id.
    with pytest.raises(IntegrityError, match="operation_log_id"), db.begin_nested():
        db.execute(text(COMPLETE), complete | {"log": foreign})
    db.execute(text(COMPLETE), complete)
    # Cancel metadata stays NULL on a completed job.
    with pytest.raises(IntegrityError, match="cancel_metadata"), db.begin_nested():
        db.execute(
            text("UPDATE planning_job SET cancelled_by='user:1',cancelled_at=now() WHERE id=:id"),
            {"id": job},
        )
    # One completion per idempotency key per farm.
    with pytest.raises(IntegrityError, match="ix_planning_job_farm_request_key"), db.begin_nested():
        db.execute(text(COMPLETE), complete | {"id": other})
    # The linked operation cannot be deleted from under its completed job.
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("DELETE FROM log WHERE id=:id"), {"id": mine})


@pytest.mark.parametrize(
    ("action", "status", "valid"),
    [
        ("completed", "completed", True),
        ("cancelled", "cancelled", True),
        ("edited", "planned", True),
        ("completed", "planned", False),
        ("edited", "completed", False),
        ("cancelled", "completed", False),
    ],
)
def test_revision_actions_imply_their_status(database, action, status, valid):
    db = database
    first, _, args = _planning_fixture(db)
    job = planned_job(db, first, **args)
    insert = text(
        "INSERT INTO planning_job_revision(farm_id,job_id,revision,action,status,snapshot,actor) "
        "VALUES (:farm,:job,1,:action,:status,'{}'::jsonb,'user:1')"
    )
    values = {"farm": first, "job": job, "action": action, "status": status}
    if valid:
        db.execute(insert, values)
    else:
        with pytest.raises(IntegrityError, match="action_status"), db.begin_nested():
            db.execute(insert, values)


def test_job_completion_downgrade_refuses_completed_history_then_round_trips(scratch_db_url):
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            first, _, args = _planning_fixture(db)
            job = planned_job(db, first, **args)
            log = spray_log(db, first)
            db.execute(
                text(
                    "INSERT INTO log_source(log_id,source_type,external_key,precision_rank) "
                    "VALUES (:log,'planning','job:1',1)"
                ),
                {"log": log},
            )
            db.execute(text(COMPLETE), {"id": job, "by": "user:1", "log": log, "key": "k1"})
        with pytest.raises(RuntimeError, match="downgrade refused"):
            command.downgrade(cfg, "planning_0002")
        with engine.connect() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
                "planning_0003"
            )
            assert db.execute(text("SELECT status FROM planning_job")).scalar() == "completed"
        with engine.begin() as db:
            db.execute(text("DELETE FROM planning_job"))
            db.execute(text("DELETE FROM log_source"))
        command.downgrade(cfg, "planning_0002")
        with engine.connect() as db:
            columns = db.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='planning_job' AND column_name='operation_log_id'"
                )
            ).scalar_one()
            assert columns == 0
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        engine.dispose()
