"""core: dated crop seasons on a stable parcel

A `crop_season` row gives a parcel an inclusive `[starts_on, ends_on]` crop context; a null
end means the season is open. Null crop, crop name or BBCH stage means unknown. A farm-scoped
composite foreign key keeps each season on a parcel of its own farm, and a GiST exclusion
constraint rejects overlapping seasons of one parcel atomically (SQLSTATE 23P01, `btree_gist`
for bigint equality). Purely additive: existing parcels gain no season, so no crop fact is
inferred from legacy parcel props.

Revision ID: core_0005
Revises: planning_0003
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0005"
down_revision: str | None = "planning_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "crop_season",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("parcel_id", sa.BigInteger(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("crop_eppo", sa.Text(), nullable=True),
        sa.Column("crop_name", sa.Text(), nullable=True),
        sa.Column("bbch", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ends_on IS NULL OR ends_on >= starts_on", name=op.f("ck_crop_season_dates")
        ),
        sa.CheckConstraint(
            "crop_eppo IS NULL OR crop_eppo ~ '^[0-9A-Z]{5,6}$'",
            name=op.f("ck_crop_season_crop_eppo"),
        ),
        sa.CheckConstraint(
            "crop_name IS NULL OR length(btrim(crop_name)) BETWEEN 1 AND 200",
            name=op.f("ck_crop_season_crop_name"),
        ),
        sa.CheckConstraint(
            "bbch IS NULL OR bbch BETWEEN 0 AND 99", name=op.f("ck_crop_season_bbch")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_crop_season_revision")),
        sa.ForeignKeyConstraint(
            ["parcel_id", "farm_id"],
            ["parcel.id", "parcel.farm_id"],
            name=op.f("fk_crop_season_parcel_id_parcel"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crop_season")),
    )
    op.execute(
        "ALTER TABLE crop_season ADD CONSTRAINT ex_crop_season_parcel_dates "
        "EXCLUDE USING gist (parcel_id WITH =, daterange(starts_on, ends_on, '[]') WITH &&)"
    )
    op.create_index("ix_crop_season_parcel_id_starts_on", "crop_season", ["parcel_id", "starts_on"])


def downgrade() -> None:
    # Seasons are owner-entered facts the earlier schema cannot hold.
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM crop_season")).scalar_one()
    if count:
        raise RuntimeError(
            f"core_0005 downgrade refused: {count} crop season(s) exist; back up and remove them "
            "first"
        )
    op.drop_table("crop_season")  # its index and exclusion constraint go with it
    # btree_gist stays, as in planning_0002: other objects rely on it.
