"""core_0018 moves spray products and doses onto the catalog tables (the single copy since).

Failure modes owned here:
- Meaning drift on a populated upgrade: a product reads with another name, authorisation,
  substance, label props or crop-protection attributes; a dose with another value, unit, measure
  or label, or a stock reader converts it differently; an id kept outside the two tables (stock,
  planning, shortlist, ext_id, open draft, a published NGSI-LD v1 id) names another item, even
  when legacy product ids collide with catalog item ids already present.
- A lossy or partial upgrade: a row the new checks reject is coerced, or the upgrade stops
  half way; every blocker is listed and nothing changes.
- A lossy downgrade: one that drops a spray recorded or changed since the upgrade, or one that
  does not return the legacy rows exactly.
"""

from __future__ import annotations

import json

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from farmcore.apps.advisor import products as advisor_products
from farmcore.apps.operations import drafts as operation_drafts
from farmcore.apps.operations import spray_items
from farmcore.apps.share import ngsi
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.commands.migrate import alembic_config
from farmcore.core.models.canonical import Farm
from tests.core.test_revamp_migrations import farm, planned_job, scratch_db_url  # noqa: F401

BEFORE = "core_0017"
LEGACY = {  # what the frozen tables and the ids outside them hold, by content
    "product": "SELECT id, name, authorisation_no, active_substance, product_type, targets, "
    "props::text FROM product ORDER BY id",
    "quantity": "SELECT id, log_id, product_id, measure, value::text, unit, label FROM quantity "
    "ORDER BY id",
    "stock": "SELECT id, product_id, catalog_item_id, purchased::text FROM inventory_stock "
    "ORDER BY id",
    "planning": "SELECT id, product_id FROM planning_job ORDER BY id",
    "farm": "SELECT id, props::text FROM farm ORDER BY id",
    "ext_id": "SELECT source, key, model, res_id FROM ext_id ORDER BY id",
    "draft": "SELECT id, payload::text FROM draft ORDER BY id",
    "view": "SELECT legacy_quantity_id, log_id, product_id, measure, value::text, unit, "
    "entered_value::text, entered_unit FROM legacy_typed_quantity ORDER BY 1",
}
# The `legacy_typed_quantity` view's rows, as the moved typed quantities give them back.
MOVED_VIEW = (
    "SELECT m.quantity_id, t.log_id, pm.product_id, t.measure, t.value::text, t.unit, "
    "t.entered_value::text, t.entered_unit FROM typed_quantity t "
    "JOIN legacy_quantity_map m ON m.typed_quantity_id = t.id "
    "LEFT JOIN legacy_product_map pm ON pm.catalog_item_id = t.catalog_item_id ORDER BY 1"
)


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


def _legacy(db) -> dict[str, int]:
    """A core_0017 farm with every place a product or dose id lives. Two E-Phy catalog items
    come first, so the legacy product ids collide with catalog item ids."""
    for name in ("EPHY ONE", "EPHY TWO"):
        _one(db, "INSERT INTO catalog_item(name) VALUES (:n) RETURNING id", n=name)
    farm_id = farm(db)
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
    planned_job(db, farm_id, advice, copper)
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
    return {"farm": farm_id, "copper": copper, "pending": pending, "spray": spray}


def _capture(db) -> dict[str, list]:
    return {name: db.execute(text(sql)).all() for name, sql in LEGACY.items() if name != "view"}


def test_a_populated_upgrade_keeps_every_meaning_and_id(scratch_db_url):  # noqa: F811
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, BEFORE)
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            ids = _legacy(db)
            view = db.execute(text(LEGACY["view"])).all()
            legacy = _capture(db)
        command.upgrade(cfg, "head")
        command.check(cfg)

        with engine.begin() as db:
            assert db.execute(text(MOVED_VIEW)).all() == view  # what stock read, unchanged
            item = dict(
                db.execute(text("SELECT product_id, catalog_item_id FROM legacy_product_map"))
                .tuples()
                .all()
            )
            assert db.execute(
                text("SELECT name FROM catalog_item ORDER BY id")
            ).scalars().all() == [
                "EPHY ONE",
                "EPHY TWO",
                "Copper",
                "Pending AMM",
            ]
            assert db.execute(text(LEGACY["product"])).all() == legacy["product"]  # frozen
            assert _one(db, "SELECT catalog_item_id FROM inventory_stock") == item[ids["copper"]]
            assert _one(db, "SELECT catalog_item_id FROM planning_job") == item[ids["copper"]]
            assert _one(db, "SELECT model || ':' || res_id FROM ext_id") == (
                f"catalog_item:{item[ids['copper']]}"
            )
        with Session(engine) as session:
            farm_row = session.get(Farm, ids["farm"])
            assert farm_row.props == {
                "catalog_item_ids": [item[ids["pending"]], item[ids["copper"]]]
            }
            repo = ScopedRepository(
                session, farm_row.id, ensure_owner_grant(session, farm_row), "test", "upgrade"
            )
            items = {i.name: i for i in spray_items.items_for_farm(repo, scope="logs:read")}
            assert [
                (i.authorisation_no, i.active_substance, dict(i.props)) for i in items.values()
            ] == [
                (p.authorisation_no, p.active_substance, json.loads(p.props))
                for p in sorted(legacy["product"], key=lambda p: p.name)
            ]
            assert items["Pending AMM"].placeholder
            refs = advisor_products.product_refs(repo, item.values(), scope="logs:read")
            assert {(r.name, r.product_type, tuple(r.targets)) for r in refs.values()} == {
                ("Copper", "contact", ("downy",)),
                ("Pending AMM", "penetrant", ("powdery",)),
            }
            doses = spray_items.doses_for_logs(repo, [ids["spray"]], scope="logs:read")
            assert [(d.measure, str(d.value), d.unit, d.label) for d in doses[ids["spray"]]] == [
                (q.measure, q.value, q.unit, q.label) for q in legacy["quantity"]
            ]
            published = ngsi.refs(repo, doses[ids["spray"]], version=1)
            assert sorted(published.quantities.values()) == [q.id for q in legacy["quantity"]]
            assert published.products == {v: k for k, v in item.items()}
            [draft] = session.execute(text("SELECT payload FROM draft")).scalars().all()
            record = operation_drafts.validate_spray(session, farm_row.id, draft)
            assert [d["catalog_item_id"] for d in record["doses"]] == [item[ids["copper"]]]
    finally:
        engine.dispose()


def test_the_preflight_lists_every_blocker_and_changes_nothing(scratch_db_url):  # noqa: F811
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, BEFORE)
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            ids = _legacy(db)
            blank = _product(db, " ", "x", "contact", [], {})
            nan = _dose(db, ids["spray"], blank, "rate", "NaN", "kg/ha", None)
            unitless = _dose(db, ids["spray"], None, "rate", "1", " ", None)
            parcel = _one(db, "SELECT parcel_id FROM log_parcel LIMIT 1")
            water = _log(db, ids["farm"], parcel, type="irrigate")
            _dose(db, water, None, "volume", "20", "m3", None)  # before #92, beside the entered
            db.execute(
                text(
                    "INSERT INTO typed_quantity(log_id,entered_value,entered_unit) "
                    "VALUES (:log,20,'m3')"
                ),
                {"log": water},
            )
            before = _capture(db)

        with pytest.raises(RuntimeError) as refused:
            command.upgrade(cfg, "head")

        message = str(refused.value)
        for blocker in (
            f"1 product(s) with a blank name or one over 200 characters ({blank})",
            f"1 quantit(y/ies) with a NaN value ({nan})",
            f"1 quantit(y/ies) with a blank unit ({unitless})",
            "1 irrigation(s) that would hold more than one amount (a legacy quantity beside the "
            f"entered one) ({water})",
        ):
            assert blocker in message
        with engine.begin() as db:
            assert _one(db, "SELECT version_num FROM alembic_version") == BEFORE
            assert _one(db, "SELECT to_regclass('legacy_product_map')::text") is None
            assert _capture(db) == before
    finally:
        engine.dispose()


def test_a_downgrade_refuses_new_or_changed_sprays_and_else_returns_every_row(
    scratch_db_url,  # noqa: F811
):
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, BEFORE)
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            ids = _legacy(db)
            before = _capture(db)
        command.upgrade(cfg, "head")
        with engine.begin() as db:
            moved = db.execute(text(MOVED_VIEW)).all()
            first = _one(db, "SELECT min(typed_quantity_id) FROM legacy_quantity_map")
            db.execute(
                text("UPDATE typed_quantity SET entered_value = 9 WHERE id = :t"), {"t": first}
            )
            db.execute(
                text(
                    "INSERT INTO typed_quantity(log_id,entered_value,entered_unit,"
                    "entered_measure) VALUES (:log,1,'kg/ha','rate')"
                ),
                {"log": ids["spray"]},
            )

        with pytest.raises(RuntimeError) as refused:
            command.downgrade(cfg, BEFORE)

        assert "1 spray dose(s)" in str(refused.value)
        assert "1 moved dose(s) changed" in str(refused.value)
        with engine.begin() as db:
            assert _one(db, "SELECT version_num FROM alembic_version") == "core_0018"
            db.execute(text("DELETE FROM typed_quantity WHERE entered_value = 1"))
            db.execute(
                text("UPDATE typed_quantity SET entered_value = 800 WHERE id = :t"), {"t": first}
            )
        command.downgrade(cfg, BEFORE)
        with engine.begin() as db:
            assert _capture(db) == before
            assert _one(db, "SELECT count(*) FROM typed_quantity") == 0
        command.upgrade(cfg, "head")
        with engine.begin() as db:
            assert db.execute(text(MOVED_VIEW)).all() == moved
    finally:
        engine.dispose()
