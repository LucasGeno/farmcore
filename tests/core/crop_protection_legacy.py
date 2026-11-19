"""A populated core_0017 database for the core_0018 move, and what the app renders from it.

Imports only what both the release before the move and this one have, so the BEFORE golden
(`snapshots/crop_protection_move_before.json`) is rendered by the release before the move from
the same rows: regenerate it there with `python -m tests.core.crop_protection_legacy <db url>`
on a database at core_0017.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from farmcore.apps.fertilization import records as fertilization_records
from farmcore.apps.fertilization import views as fertilization_views
from farmcore.apps.operations import facts, register
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm, Log

NOW = datetime(2024, 6, 1, 4, tzinfo=UTC)
TZ = "Europe/Paris"


def _one(db, sql, **params):
    return db.execute(text(sql), params).scalar_one()


def _log(db, farm_id, parcel, type="spray"):
    log = _one(
        db,
        "INSERT INTO log(farm_id,type,status,name,ts_start) VALUES "
        "(:farm,:type,'done','x','2024-04-10T05:00:00+00:00') RETURNING id",
        farm=farm_id,
        type=type,
    )
    db.execute(
        text("INSERT INTO log_parcel(log_id,parcel_id) VALUES (:log,:parcel)"),
        {"log": log, "parcel": parcel},
    )
    return log


def _product(db, name, substance, type_, targets, props):
    return _one(
        db,
        "INSERT INTO product(name,authorisation_no,active_substance,product_type,targets,props) "
        "VALUES (:name,:amm,:substance,:type,:targets,CAST(:props AS jsonb)) RETURNING id",
        name=name,
        amm=f"AMM-{name}",
        substance=substance,
        type=type_,
        targets=targets,
        props=json.dumps(props),
    )


def _dose(db, log, product, measure, value, unit, label):
    return _one(
        db,
        "INSERT INTO quantity(log_id,product_id,measure,value,unit,label) "
        "VALUES (:log,:product,:measure,CAST(:value AS numeric),:unit,:label) RETURNING id",
        log=log,
        product=product,
        measure=measure,
        value=value,
        unit=unit,
        label=label,
    )


def legacy(db) -> dict[str, int]:
    """A core_0017 farm with every place a product or dose id lives. Two E-Phy catalog items
    come first, so the legacy product ids collide with catalog item ids."""
    for name in ("EPHY ONE", "EPHY TWO"):
        _one(db, "INSERT INTO catalog_item(name) VALUES (:n) RETURNING id", n=name)
    farm_id = _one(db, "INSERT INTO farm(name) VALUES ('A') RETURNING id")
    parcel = _one(
        db,
        "INSERT INTO parcel(farm_id,name,alias,area_ha,geom) VALUES (:farm,'P','P1',2,"
        "ST_Multi(ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))',4326))) RETURNING id",
        farm=farm_id,
    )
    copper = _product(
        db,
        "Copper",
        "copper",
        "contact",
        ["downy"],
        {"placeholder": False, "label_max_dose_kg_ha": "3.75", "organic": {"source": "x"}},
    )
    pending = _product(
        db, "Pending AMM", "sulphur", "penetrant", ["powdery"], {"placeholder": True}
    )
    spray = _log(db, farm_id, parcel)
    _dose(db, spray, copper, "rate", "800", "g/ha", "Copper")
    _dose(db, spray, pending, "rate", "2", "L/ha", "Pending AMM")
    _dose(db, spray, None, "rate", "3", "bottles/ha", "Mystery")
    _dose(db, spray, copper, "volume", "200", "L", "Tank")
    db.execute(
        text(
            "INSERT INTO inventory_stock(farm_id,product_id,season,unit,purchased) "
            "VALUES (:farm,:product,2024,'kg',20)"
        ),
        {"farm": farm_id, "product": copper},
    )
    advice = _one(
        db,
        "INSERT INTO advice(farm_id,parcel_id,evaluated_at,decision) "
        "VALUES (:farm,:parcel,now(),'spray') RETURNING id",
        farm=farm_id,
        parcel=parcel,
    )
    db.execute(
        text(
            "INSERT INTO planning_job (farm_id,name,assessment_id,config_version_status,"
            "product_id,scheduled_start,duration_minutes,created_by,updated_by) VALUES "
            "(:farm,'Spray block A',:advice,'approved',:product,now(),90,'u','u')"
        ),
        {"farm": farm_id, "advice": advice, "product": copper},
    )
    # Records of other types from before #92: an amount only in `quantity` (no entered one).
    compost = _log(db, farm_id, parcel, type="fertilise")
    _dose(db, compost, None, "rate", "200", "kg/ha", "Compost")
    water = _log(db, farm_id, parcel, type="irrigate")
    _dose(db, water, None, "volume", "30", "m3", None)
    db.execute(
        text("UPDATE farm SET props = CAST(:p AS jsonb) WHERE id = :f"),
        {"f": farm_id, "p": json.dumps({"product_ids": [pending, copper]})},
    )
    db.execute(text("INSERT INTO source(code,name,kind,licence) VALUES ('demo','d','seed','x')"))
    db.execute(
        text("INSERT INTO ext_id(source,key,model,res_id) VALUES ('demo','copper','product',:p)"),
        {"p": copper},
    )
    db.execute(
        text(
            "INSERT INTO draft(farm_id,actor,kind,request_key,payload,provenance,expires_at) "
            "VALUES (:farm,'agent','spray','k',CAST(:payload AS jsonb),'{}',now() + interval "
            "'1 day')"
        ),
        {
            "farm": farm_id,
            "payload": json.dumps(
                {
                    "parcel_ids": [parcel],
                    "ts_start": "2024-04-10T05:00:00+00:00",
                    "doses": [{"product_id": copper, "value": "2", "unit": "kg/ha"}],
                }
            ),
        },
    )
    return {
        "farm": farm_id,
        "copper": copper,
        "pending": pending,
        "spray": spray,
        "compost": compost,
        "water": water,
    }


def render(session: Session, farm_id: int) -> dict:
    """The owner's register JSON, the fertilisers offered, the fertilization rows and every
    record's one-line fact, as JSON text (what the pages show)."""
    farm = session.get(Farm, farm_id)
    repo = ScopedRepository(session, farm_id, ensure_owner_grant(session, farm), "test", "render")
    rows = register.register_rows(repo, NOW, tz=TZ, checks=True, weather=True, aliases=True)
    logs = session.scalars(select(Log).where(Log.farm_id == farm_id).order_by(Log.id)).all()
    out = {
        "register_json": json.dumps(rows),
        "offered_fertilisers": sorted(
            name for _, name in session.execute(fertilization_records.offered_items(farm_id))
        ),
        "fertilization_rows": [
            [r.fertiliser, r.amount, r.per_ha]
            for r in fertilization_views.season_rows(repo, now=NOW, tz=TZ)
        ],
        "record_facts": [[log.type, facts.summary(log)] for log in logs],
        "record_products": {
            str(k): v for k, v in register.product_summaries(repo, logs, scope="logs:read").items()
        },
    }
    session.rollback()  # the owner grant written above is not part of the comparison
    return out


if __name__ == "__main__":  # the BEFORE golden, from a database at core_0017
    engine = create_engine(sys.argv[1])
    with engine.begin() as db:
        ids = legacy(db)
    with Session(engine) as session:
        print(json.dumps(render(session, ids["farm"]), indent=2, sort_keys=True))
