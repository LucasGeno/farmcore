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
