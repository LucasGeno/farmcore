"""inventory: inventory_stock (one row per farm, product and season)

Revision ID: inventory_0001
Revises: forecast_0001
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inventory_0001"
down_revision: str | None = "forecast_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inventory_stock",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("purchased", sa.Numeric(), nullable=False),
        sa.Column("lot", sa.Text(), nullable=True),
        sa.Column("last_purchase_on", sa.Date(), nullable=True),
        sa.Column("low_stock", sa.Numeric(), nullable=True),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "low_stock IS NULL OR low_stock >= 0", name=op.f("ck_inventory_stock_low_stock")
        ),
        sa.CheckConstraint("purchased >= 0", name=op.f("ck_inventory_stock_purchased")),
        sa.CheckConstraint("unit IN ('kg', 'l')", name=op.f("ck_inventory_stock_unit")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_inventory_stock_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], name=op.f("fk_inventory_stock_product_id_product")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_stock")),
        sa.UniqueConstraint(
            "farm_id",
            "product_id",
            "season",
            name=op.f("uq_inventory_stock_farm_id_product_id_season"),
        ),
    )


def downgrade() -> None:
    op.drop_table("inventory_stock")
