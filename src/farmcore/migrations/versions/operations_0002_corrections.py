"""Retained operation correction intent and terminal receipts.

Revision ID: operations_0002
Revises: core_0004_grant_recipients
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "operations_0002"
down_revision: str | None = "core_0004_grant_recipients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_correction",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("original_log_id", sa.BigInteger(), nullable=False),
        sa.Column("live_log_id", sa.BigInteger(), nullable=True),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("actor_key", sa.String(length=128), nullable=False),
        sa.Column("actor_label", sa.Text(), nullable=False),
        sa.Column("creator_identity_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("base_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_actor_key", sa.Text(), nullable=True),
        sa.Column("finalized_actor_label", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending' AND finalized_at IS NULL "
            "AND finalized_actor_key IS NULL AND finalized_actor_label IS NULL) "
            "OR (status <> 'pending' AND finalized_at IS NOT NULL "
            "AND finalized_actor_key IS NOT NULL AND finalized_actor_label IS NOT NULL)",
            name=op.f("ck_operation_correction_terminal"),
        ),
        sa.CheckConstraint(
            "base_fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("ck_operation_correction_fingerprint")
        ),
        sa.CheckConstraint(
            "btrim(actor_key) <> ''", name=op.f("ck_operation_correction_actor_key")
        ),
        sa.CheckConstraint(
            "btrim(actor_label) <> ''", name=op.f("ck_operation_correction_actor_label")
        ),
        sa.CheckConstraint("btrim(reason) <> ''", name=op.f("ck_operation_correction_reason")),
        sa.CheckConstraint(
            "btrim(request_key) <> ''", name=op.f("ck_operation_correction_request_key")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(after_snapshot) = 'object'",
            name=op.f("ck_operation_correction_after_snapshot"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before_snapshot) = 'object'",
            name=op.f("ck_operation_correction_before_snapshot"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','cancelled')",
            name=op.f("ck_operation_correction_status"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_identity_id"],
            ["local_identity.id"],
            name=op.f("fk_operation_correction_creator_identity_id_local_identity"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_operation_correction_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["live_log_id"],
            ["log.id"],
            name=op.f("fk_operation_correction_live_log_id_log"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_correction")),
        sa.UniqueConstraint(
            "farm_id",
            "actor_key",
            "request_key",
            name=op.f("uq_operation_correction_farm_id_actor_key_request_key"),
        ),
    )
    op.create_index(
        op.f("ix_operation_correction_creator_identity_id"),
        "operation_correction",
        ["creator_identity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_operation_correction_farm_id"), "operation_correction", ["farm_id"], unique=False
    )
    op.create_index(
        op.f("ix_operation_correction_live_log_id"),
        "operation_correction",
        ["live_log_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_operation_correction_live_log_id"), table_name="operation_correction")
    op.drop_index(op.f("ix_operation_correction_farm_id"), table_name="operation_correction")
    op.drop_index(
        op.f("ix_operation_correction_creator_identity_id"), table_name="operation_correction"
    )
    op.drop_table("operation_correction")
