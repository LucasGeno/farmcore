"""inventory: stock of a neutral catalog item (a fertiliser) beside product stock

`inventory_stock` rows name either a crop-protection `product` (as before) or a neutral
`catalog_item`, never both: `product_id` becomes nullable, `catalog_item_id` is added, and a
check keeps exactly one of them. A catalog item has one row per farm and season, as a product has.
Existing rows keep their product and are untouched.

Downgrade refuses while any row names a catalog item: dropping the column would lose stock a
farmer entered. Delete those rows first if that is really meant.

Revision ID: inventory_0002
Revises: core_0012
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "inventory_0002"
down_revision: str | None = "core_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("inventory_stock", sa.Column("catalog_item_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_inventory_stock_catalog_item_id_catalog_item"),
        "inventory_stock",
        "catalog_item",
        ["catalog_item_id"],
        ["id"],
    )
    op.alter_column("inventory_stock", "product_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        op.f("ck_inventory_stock_item"),
        "inventory_stock",
        "num_nonnulls(product_id, catalog_item_id) = 1",
    )
    op.create_unique_constraint(
        op.f("uq_inventory_stock_farm_id_catalog_item_id_season"),
        "inventory_stock",
        ["farm_id", "catalog_item_id", "season"],
    )


def downgrade() -> None:
    held = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM inventory_stock WHERE catalog_item_id IS NOT NULL")
    )
    if held:
        raise RuntimeError(
            f"inventory_0002 downgrade refused: {held} stock rows name a catalog item and would "
            "be lost. Delete them first if that is meant."
        )
    op.drop_constraint(
        op.f("uq_inventory_stock_farm_id_catalog_item_id_season"), "inventory_stock", "unique"
    )
    op.drop_constraint(op.f("ck_inventory_stock_item"), "inventory_stock", "check")
    op.alter_column("inventory_stock", "product_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_constraint(
        op.f("fk_inventory_stock_catalog_item_id_catalog_item"), "inventory_stock", "foreignkey"
    )
    op.drop_column("inventory_stock", "catalog_item_id")
