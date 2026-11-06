"""forecast: forecast_availability (farm-scoped Busy/Free per local day, replaces the cookie)

Revision ID: forecast_0002
Revises: review_0001
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "forecast_0002"
down_revision: str | None = "review_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_availability",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "day BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'",
            name=op.f("ck_forecast_availability_day"),
        ),
        sa.CheckConstraint("length(notes) <= 1000", name=op.f("ck_forecast_availability_notes")),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0", name=op.f("ck_forecast_availability_created_by")
        ),
        sa.CheckConstraint(
            "length(btrim(updated_by)) > 0", name=op.f("ck_forecast_availability_updated_by")
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_forecast_availability_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecast_availability")),
        sa.UniqueConstraint("farm_id", "day", name=op.f("uq_forecast_availability_farm_id_day")),
    )


def downgrade() -> None:
    op.drop_table("forecast_availability")
