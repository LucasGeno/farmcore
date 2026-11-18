"""core: hashed bearer tokens bound to a data grant (personal access tokens)

Adds `access_token`: the SHA-256 of a random token, its kind (`personal` for now), the
`data_grant` it authenticates as (farm, scopes, validity and revocation live there), its issuer
and when it was last used. Purely additive; no existing row changes.

Revision ID: core_0007
Revises: core_0006
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0007"
down_revision: str | None = "core_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_token",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("grant_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_identity_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_access_token_token_hash")
        ),
        sa.CheckConstraint("kind IN ('personal')", name=op.f("ck_access_token_kind")),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["data_grant.id"], name=op.f("fk_access_token_grant_id_data_grant")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_identity_id"],
            ["local_identity.id"],
            name=op.f("fk_access_token_created_by_identity_id_local_identity"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_access_token_token_hash")),
        sa.UniqueConstraint("grant_id", name=op.f("uq_access_token_grant_id")),
    )
    op.create_index(
        op.f("ix_access_token_created_by_identity_id"),
        "access_token",
        ["created_by_identity_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_access_token_created_by_identity_id"), table_name="access_token")
    op.drop_table("access_token")
