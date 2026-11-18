"""catalog_fr_ephy: France's E-Phy registered uses and the import fingerprint

Adds `catalog_use` (one row per E-Phy usage line: crop, target, max dose, interval, max
applications, normalised state) and `catalog_import` (the source, licence, retrieved time and
content hash that make an unchanged re-import write nothing). Additive: the neutral
`catalog_item` and `catalog_authorisation` tables come from core_0006.

Revision ID: catalog_fr_0001
Revises: core_0007
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "catalog_fr_0001"
down_revision: str | None = "core_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_use",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("identifiant", sa.Text(), nullable=False),
        sa.Column("crop", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("max_dose", sa.Numeric(), nullable=True),
        sa.Column("max_dose_unit", sa.Text(), nullable=True),
        sa.Column("interval_days", sa.Numeric(), nullable=True),
        sa.Column("max_applications", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(crop)) BETWEEN 1 AND 200", name=op.f("ck_catalog_use_crop")
        ),
        sa.CheckConstraint(
            "state IN ('authorised', 'withdrawn', 'provisional')",
            name=op.f("ck_catalog_use_state"),
        ),
        sa.CheckConstraint(
            "max_dose IS NULL OR max_dose > 0", name=op.f("ck_catalog_use_max_dose")
        ),
        sa.CheckConstraint(
            "interval_days IS NULL OR interval_days > 0", name=op.f("ck_catalog_use_interval_days")
        ),
        sa.CheckConstraint(
            "max_applications IS NULL OR max_applications > 0",
            name=op.f("ck_catalog_use_max_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_catalog_use_catalog_item_id_catalog_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_use")),
    )
    op.create_index("ix_catalog_use_catalog_item_id", "catalog_use", ["catalog_item_id"])
    op.create_table(
        "catalog_import",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("licence", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_catalog_import_content_hash")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_import")),
        sa.UniqueConstraint(
            "source", "content_hash", name=op.f("uq_catalog_import_source_content_hash")
        ),
    )


def downgrade() -> None:
    op.drop_table("catalog_import")
    op.drop_index("ix_catalog_use_catalog_item_id", table_name="catalog_use")
    op.drop_table("catalog_use")
