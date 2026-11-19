"""core: OAuth clients registered without the operator (RFC 7591, client ID metadata documents)

Adds `oauth_client`: clients that registered themselves (`dynamic`) and the last valid client ID
metadata document fetched for a URL client ID (`metadata`, a cache). Additive. A downgrade drops
it: connections of those clients can no longer refresh (their client is unknown) and their
access tokens expire within the hour.

Revision ID: core_0016
Revises: catalog_eu_0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0016"
down_revision: str | None = "catalog_eu_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_client",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("registration", sa.String(16), nullable=False),
        sa.Column("client_name", sa.String(80), nullable=False),
        sa.Column("redirect_uris", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("peer_slot", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "registration IN ('dynamic', 'metadata')", name=op.f("ck_oauth_client_registration")
        ),
        sa.CheckConstraint(
            "(registration = 'dynamic' AND fresh_until IS NULL AND client_id !~ '^https://')"
            " OR (registration = 'metadata' AND fresh_until IS NOT NULL AND peer_slot IS NULL)",
            name=op.f("ck_oauth_client_registration_fields"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_client")),
        sa.UniqueConstraint("client_id", name=op.f("uq_oauth_client_client_id")),
    )


def downgrade() -> None:
    op.drop_table("oauth_client")
