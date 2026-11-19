"""core: a transactional outbox for deliveries outside the installation

Adds `outbox`: one row per delivery intent (today a Telegram advice digest to one chat), written
in the transaction of its cause and dispatched by `farmcore worker` (`core/outbox.py`): farm,
source app, kind, a unique dedupe key naming the cause, payload, state (pending, sending, sent,
failed, ambiguous, expired), claim count, wall-clock availability and expiry, claim and finish
times, the provider's receipt and an error type. Additive: no existing row changes, and a
downgrade drops the table with whatever it holds.

Revision ID: core_0013
Revises: inventory_0002
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0013"
down_revision: str | None = "inventory_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("source_app", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed', 'ambiguous', 'expired')",
            name=op.f("ck_outbox_state"),
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=op.f("ck_outbox_payload")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_outbox_farm_id_farm"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_outbox_dedupe_key")),
    )
    op.create_index(op.f("ix_outbox_farm_id"), "outbox", ["farm_id"])
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["available_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("outbox")
