"""kernel: postgis extension, app_module, clock_state

Revision ID: core_0001
Revises:
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "app_module",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="installed", nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('installed', 'uninstalled')", name=op.f("ck_app_module_state")
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_app_module")),
    )
    op.create_table(
        "clock_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="replay", nullable=False),
        sa.Column("replay_now", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "job_runs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_clock_state_single_row")),
        sa.CheckConstraint("mode IN ('replay', 'live')", name=op.f("ck_clock_state_mode")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clock_state")),
    )


def downgrade() -> None:
    op.drop_table("clock_state")
    op.drop_table("app_module")
    # The postgis extension is left in place: other tables or databases may rely on it.
