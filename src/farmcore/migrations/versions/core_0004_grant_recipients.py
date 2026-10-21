"""Explicit recipient identity for grants; legacy grants remain unbound.

Revision ID: core_0004_grant_recipients
Revises: core_0003
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0004_grant_recipients"
down_revision: str | None = "core_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("data_grant", sa.Column("recipient_identity_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_data_grant_recipient_identity_id_local_identity"),
        "data_grant",
        "local_identity",
        ["recipient_identity_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_data_grant_recipient_identity_id"),
        "data_grant",
        ["recipient_identity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_data_grant_recipient_identity_id"), table_name="data_grant")
    op.drop_constraint(
        op.f("fk_data_grant_recipient_identity_id_local_identity"),
        "data_grant",
        type_="foreignkey",
    )
    op.drop_column("data_grant", "recipient_identity_id")
