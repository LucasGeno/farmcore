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
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from farmcore.apps.advisor import products as advisor_products
from farmcore.apps.operations import drafts as operation_drafts
from farmcore.apps.operations import spray_items
from farmcore.apps.share import ngsi
from farmcore.cli import cli
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.commands import migrate as migrate_command
from farmcore.core.commands.migrate import alembic_config
from farmcore.core.models.canonical import Farm
from tests.core import crop_protection_legacy as fixture
from tests.core.crop_protection_legacy import _dose, _log, _one, _product
from tests.core.test_revamp_migrations import scratch_db_url  # noqa: F401

BEFORE = "core_0017"
# Rendered by the release before the move (`crop_protection_legacy` says how to regenerate it).
BEFORE_RENDER = Path(__file__).parent / "snapshots" / "crop_protection_move_before.json"
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


def _capture(db) -> dict[str, list]:
    return {name: db.execute(text(sql)).all() for name, sql in LEGACY.items() if name != "view"}


def test_a_populated_upgrade_keeps_every_meaning_and_id(scratch_db_url):  # noqa: F811
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, BEFORE)
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            ids = fixture.legacy(db)
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
            # What the pages render, byte for byte as the release before the move rendered it
            # from the same rows: register.json (check keys too), the fertilisers offered, the
            # fertilization rows, each record's fact and products.
            assert fixture.render(session, ids["farm"]) == json.loads(BEFORE_RENDER.read_text())
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
                (q.measure, q.value, q.unit, q.label)
                for q in legacy["quantity"]
                if q.log_id == ids["spray"]
            ]
            published = ngsi.refs(repo, doses[ids["spray"]], version=1)
            assert sorted(published.quantities.values()) == [
                q.id for q in legacy["quantity"] if q.log_id == ids["spray"]
            ]
            assert published.products == {v: k for k, v in item.items()}
            [draft] = session.execute(text("SELECT payload FROM draft")).scalars().all()
            record = operation_drafts.validate_spray(session, farm_row.id, draft)
            assert [d["catalog_item_id"] for d in record["doses"]] == [item[ids["copper"]]]
    finally:
        engine.dispose()


def test_migrate_lists_every_preflight_blocker_and_changes_nothing(
    scratch_db_url,  # noqa: F811
    settings,
    monkeypatch,
):
    """`farmcore migrate` prints every row the preflight refuses (ids, never row values), and
    the database is left exactly as it was."""
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, BEFORE)
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            ids = fixture.legacy(db)
            blank = _product(db, " ", "x", "contact", [], {})
            nan = _dose(db, ids["spray"], blank, "rate", "NaN", "kg/ha", None)
            infinite = _dose(db, ids["spray"], None, "rate", "Infinity", "kg/ha", None)
            unitless = _dose(db, ids["spray"], None, "rate", "1", " ", None)
            parcel = _one(db, "SELECT parcel_id FROM log_parcel LIMIT 1")
            twice = []  # before #92: a legacy quantity beside the entered amount
            for type_, value, unit in (("irrigate", "20", "m3"), ("fertilise", "300", "kg/ha")):
                log = _log(db, ids["farm"], parcel, type=type_)
                _dose(db, log, None, "rate", value, unit, "Secret label")
                db.execute(
                    text(
                        "INSERT INTO typed_quantity(log_id,entered_value,entered_unit) "
                        "VALUES (:log,100,'kg')"
                    ),
                    {"log": log},
                )
                twice.append(log)
            fractional = _one(
                db,
                "INSERT INTO farm(name,props) VALUES ('B','{\"product_ids\": [1.0]}') RETURNING id",
            )
            before = _capture(db)
        monkeypatch.setattr(
            migrate_command,
            "get_settings",
            lambda: settings.model_copy(update={"database_url": scratch_db_url}),
        )

        result = CliRunner().invoke(cli, ["migrate"])

        assert result.exit_code == 1
        for blocker in (
            f"1 product(s) with a blank name or one over 200 characters ({blank})",
            f"2 quantit(y/ies) with a NaN or infinite value ({nan}, {infinite})",
            f"1 quantit(y/ies) with a blank unit ({unitless})",
            f"1 farm(s) whose product shortlist is not a list of whole-number ids ({fractional})",
            "2 record(s) other than a spray that would hold more than one amount (a legacy "
            f"quantity beside another or beside the entered one) ({twice[0]}, {twice[1]})",
            f"the schema is still at {BEFORE}",
        ):
            assert blocker in result.output
        assert "Secret label" not in result.output and ".;" not in result.output
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
            ids = fixture.legacy(db)
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
            db.execute(  # a pre-#92 fertilise record corrected since: its entered amount added
                text(
                    "INSERT INTO typed_quantity(log_id,entered_value,entered_unit) "
                    "VALUES (:log,180,'kg')"
                ),
                {"log": ids["compost"]},
            )

        with pytest.raises(RuntimeError) as refused:
            command.downgrade(cfg, BEFORE)

        assert "1 spray dose(s)" in str(refused.value)
        assert "1 moved dose(s) changed" in str(refused.value)
        assert (
            "1 record(s) other than a spray corrected since the upgrade, holding a moved legacy "
            f"amount beside an entered one ({ids['compost']})"
        ) in str(refused.value)
        with engine.begin() as db:
            assert _one(db, "SELECT version_num FROM alembic_version") == "core_0018"
            db.execute(text("DELETE FROM typed_quantity WHERE entered_value IN (1, 180)"))
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
