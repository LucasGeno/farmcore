"""inventory_0002 on a populated database (failure model 6 in `test_records.py`)."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from farmcore.core.commands.migrate import alembic_config
from tests.core.test_revamp_migrations import scratch_db_url  # noqa: F401  (fixture)

STOCK = "SELECT farm_id, product_id, season, unit, purchased, lot FROM inventory_stock ORDER BY id"


def test_legacy_stock_survives_and_a_fertiliser_stock_is_never_dropped(scratch_db_url):  # noqa: F811
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "core_0012")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            farm = db.execute(text("INSERT INTO farm(name) VALUES ('A') RETURNING id")).scalar()
            product = db.execute(
                text(
                    "INSERT INTO product(name,authorisation_no,active_substance,product_type) "
                    "VALUES ('Copper','X-1','copper','contact') RETURNING id"
                )
            ).scalar()
            db.execute(
                text(
                    "INSERT INTO inventory_stock(farm_id,product_id,season,unit,purchased,lot) "
                    "VALUES (:farm,:product,2024,'kg',20.125,'L-7')"
                ),
                {"farm": farm, "product": product},
            )
            legacy = db.execute(text(STOCK)).all()
        command.upgrade(cfg, "inventory_0002")  # core_0018 later moves product stock rows
        with engine.begin() as db:
            assert db.execute(text(STOCK)).all() == legacy
            item = db.execute(
                text("INSERT INTO catalog_item(name) VALUES ('CAN 27') RETURNING id")
            ).scalar()
            db.execute(
                text(
                    "INSERT INTO inventory_stock(farm_id,catalog_item_id,season,unit,purchased) "
                    "VALUES (:farm,:item,2024,'kg',1000)"
                ),
                {"farm": farm, "item": item},
            )
        for both_or_neither in (f"{product},{item}", "NULL,NULL"):
            with pytest.raises(IntegrityError), engine.begin() as db:
                db.execute(
                    text(
                        "INSERT INTO inventory_stock(farm_id,product_id,catalog_item_id,season,"
                        f"unit,purchased) VALUES ({farm},{both_or_neither},2025,'kg',1)"
                    )
                )
        with pytest.raises(RuntimeError, match="1 stock rows name a catalog item"):
            command.downgrade(cfg, "core_0012")
        with engine.begin() as db:
            db.execute(text("DELETE FROM inventory_stock WHERE catalog_item_id IS NOT NULL"))
        command.downgrade(cfg, "core_0012")
        with engine.begin() as db:
            assert db.execute(text(STOCK)).all() == legacy
    finally:
        engine.dispose()
