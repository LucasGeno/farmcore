"""forecast: forecast_run (one per issued forecast) and forecast_value (its hourly values)

Revision ID: forecast_0001
Revises: core_0002
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "forecast_0001"
down_revision: str | None = "core_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_run",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_forecast_run_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecast_run")),
        sa.UniqueConstraint(
            "farm_id", "source", "issued_at", name=op.f("uq_forecast_run_farm_id_source_issued_at")
        ),
    )
    op.create_table(
        "forecast_value",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("variable", sa.Text(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_forecast_value_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["forecast_run.id"],
            name=op.f("fk_forecast_value_run_id_forecast_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecast_value")),
        sa.UniqueConstraint(
            "run_id", "ts", "variable", name=op.f("uq_forecast_value_run_id_ts_variable")
        ),
    )


def downgrade() -> None:
    op.drop_table("forecast_value")
    op.drop_table("forecast_run")
