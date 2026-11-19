"""catalog_eu_0001: EU active substances and the cross-catalog substance link

Adds `catalog_substance` (one row per EU-listed active substance, keyed on the normalised
`substance_key`: reference name, CAS number, approval status and approval/expiry dates, with the
remaining source fields in `props`), `catalog_substance_alias` (the exact, stripped and
`(aka …)`/`(formerly …)` keys that resolve to a substance) and `catalog_item_substance` (the link
a country catalog writes for each product's substance, keyed on `(catalog_item_id,
substance_key)`). Additive: the neutral `catalog_item` table comes from core_0006 and the shared
import fingerprint `catalog_import` from catalog_fr_0001.

Revision ID: catalog_eu_0001
Revises: core_0015
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "catalog_eu_0001"
down_revision: str | None = "core_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_substance",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("substance_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cas_number", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approval_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'not_approved', 'pending')",
            name=op.f("ck_catalog_substance_status"),
        ),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 200", name=op.f("ck_catalog_substance_name")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_substance")),
        sa.UniqueConstraint("substance_key", name=op.f("uq_catalog_substance_substance_key")),
    )
    op.create_table(
        "catalog_substance_alias",
        sa.Column("alias_key", sa.Text(), nullable=False),
        sa.Column("substance_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(alias_key)) BETWEEN 1 AND 200",
            name=op.f("ck_catalog_substance_alias_alias_key"),
        ),
        sa.ForeignKeyConstraint(
            ["substance_key"],
            ["catalog_substance.substance_key"],
            name=op.f("fk_catalog_substance_alias_substance_key_catalog_substance"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("alias_key", name=op.f("pk_catalog_substance_alias")),
    )
    op.create_index(
        "ix_catalog_substance_alias_substance_key",
        "catalog_substance_alias",
        ["substance_key"],
        unique=False,
    )
    op.create_table(
        "catalog_item_substance",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("substance_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 200", name=op.f("ck_catalog_item_substance_name")
        ),
        sa.CheckConstraint(
            "length(btrim(substance_key)) BETWEEN 1 AND 200",
            name=op.f("ck_catalog_item_substance_substance_key"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_catalog_item_substance_catalog_item_id_catalog_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_item_substance")),
        sa.UniqueConstraint(
            "catalog_item_id",
            "substance_key",
            name=op.f("uq_catalog_item_substance_catalog_item_id_substance_key"),
        ),
    )
    op.create_index(
        "ix_catalog_item_substance_substance_key",
        "catalog_item_substance",
        ["substance_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_item_substance_substance_key", table_name="catalog_item_substance")
    op.drop_table("catalog_item_substance")
    op.drop_index("ix_catalog_substance_alias_substance_key", table_name="catalog_substance_alias")
    op.drop_table("catalog_substance_alias")
    op.drop_table("catalog_substance")
