"""Local identities, explicit farm memberships, hashed sessions and bounded login buckets.

Revision ID: core_0003
Revises: inventory_0001
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0003"
down_revision: str | None = "inventory_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_identity",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "login ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'", name=op.f("ck_local_identity_login")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_local_identity")),
        sa.UniqueConstraint("login", name=op.f("uq_local_identity_login")),
    )
    op.create_table(
        "farm_membership",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("identity_id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('owner','advisor')", name=op.f("ck_farm_membership_role")),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["local_identity.id"],
            name=op.f("fk_farm_membership_identity_id_local_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_farm_membership_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_membership")),
        sa.UniqueConstraint(
            "identity_id", "farm_id", name=op.f("uq_farm_membership_identity_id_farm_id")
        ),
    )
    op.create_index(
        op.f("ix_farm_membership_identity_id"), "farm_membership", ["identity_id"], unique=False
    )
    op.create_index(
        op.f("ix_farm_membership_farm_id"), "farm_membership", ["farm_id"], unique=False
    )
    op.create_table(
        "local_session",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("identity_id", sa.BigInteger(), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_local_session_token_hash")
        ),
        sa.CheckConstraint("csrf_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_local_session_csrf_hash")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_local_session_expiry")),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["local_identity.id"],
            name=op.f("fk_local_session_identity_id_local_identity"),
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_local_session")),
    )
    op.create_index(op.f("ix_local_session_identity_id"), "local_session", ["identity_id"])
    op.create_index(op.f("ix_local_session_expires_at"), "local_session", ["expires_at"])
    op.create_table(
        "login_bucket",
        sa.Column("dimension", sa.String(16), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('account','peer','global')", name=op.f("ck_login_bucket_dimension")
        ),
        sa.CheckConstraint("slot BETWEEN 0 AND 1023", name=op.f("ck_login_bucket_slot")),
        sa.CheckConstraint(
            "dimension != 'global' OR slot = 0", name=op.f("ck_login_bucket_global_slot")
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 100", name=op.f("ck_login_bucket_attempts")),
        sa.PrimaryKeyConstraint("dimension", "slot", name=op.f("pk_login_bucket")),
    )


def downgrade() -> None:
    op.drop_table("login_bucket")
    op.drop_index(op.f("ix_local_session_expires_at"), table_name="local_session")
    op.drop_index(op.f("ix_local_session_identity_id"), table_name="local_session")
    op.drop_table("local_session")
    op.drop_index(op.f("ix_farm_membership_farm_id"), table_name="farm_membership")
    op.drop_index(op.f("ix_farm_membership_identity_id"), table_name="farm_membership")
    op.drop_table("farm_membership")
    op.drop_table("local_identity")
