"""core: one draft table for every proposed write the farmer reviews

Adds `draft`: farm, proposing actor, kind (an app's `draft_kinds` key), validated payload,
provenance, status (open, confirmed, cancelled, expired), revision, wall-clock expiry and, once
confirmed, the record it produced. Purely additive; no existing row changes.

Revision ID: core_0010
Revises: core_0009
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0010"
down_revision: str | None = "core_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by", sa.String(128), nullable=True),
        sa.Column("result_model", sa.String(32), nullable=True),
        sa.Column("result_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'confirmed', 'cancelled', 'expired')",
            name=op.f("ck_draft_status"),
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_draft_revision")),
        sa.CheckConstraint("btrim(actor) <> ''", name=op.f("ck_draft_actor")),
        sa.CheckConstraint("btrim(request_key) <> ''", name=op.f("ck_draft_request_key")),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=op.f("ck_draft_payload")),
        sa.CheckConstraint("jsonb_typeof(provenance) = 'object'", name=op.f("ck_draft_provenance")),
        sa.CheckConstraint(
            "(status = 'open' AND finalized_at IS NULL AND finalized_by IS NULL) "
            "OR (status <> 'open' AND finalized_at IS NOT NULL)",
            name=op.f("ck_draft_terminal"),
        ),
        sa.CheckConstraint(
            "(status = 'confirmed') = (result_id IS NOT NULL AND result_model IS NOT NULL)",
            name=op.f("ck_draft_result"),
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_draft_farm_id_farm")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft")),
        sa.UniqueConstraint(
            "farm_id", "actor", "request_key", name=op.f("uq_draft_farm_id_actor_request_key")
        ),
    )
    op.create_index(op.f("ix_draft_farm_id"), "draft", ["farm_id"])
    op.create_index(
        "ix_draft_open_expiry",
        "draft",
        ["expires_at"],
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_draft_open_expiry", table_name="draft")
    op.drop_index(op.f("ix_draft_farm_id"), table_name="draft")
    op.drop_table("draft")
