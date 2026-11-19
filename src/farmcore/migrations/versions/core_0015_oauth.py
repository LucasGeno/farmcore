"""core: OAuth 2.1 connections for MCP (authorization codes and rotating tokens)

`access_token` gains kind `oauth`: an agent the farm owner approved over OAuth. Such a row keeps
no token hash (its secrets rotate, in `oauth_token`) but names the client and the resource
(RFC 8707 audience) it is bound to; a `personal` row is unchanged and must keep its hash. Adds
`oauth_code` (single-use authorization codes, hashed) and `oauth_token` (hashed access and
refresh tokens of a connection). Additive: existing rows are all `personal` and satisfy the new
check; a downgrade revokes OAuth connections' grants and drops their rows, then the tables
and columns.

Revision ID: core_0015
Revises: core_0014
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0015"
down_revision: str | None = "core_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("access_token", sa.Column("client_id", sa.String(255), nullable=True))
    op.add_column("access_token", sa.Column("audience", sa.Text(), nullable=True))
    op.alter_column("access_token", "token_hash", existing_type=sa.String(64), nullable=True)
    op.drop_constraint(op.f("ck_access_token_kind"), "access_token", type_="check")
    op.create_check_constraint(
        op.f("ck_access_token_kind"), "access_token", "kind IN ('personal', 'oauth')"
    )
    op.create_check_constraint(
        op.f("ck_access_token_kind_fields"),
        "access_token",
        "(kind = 'personal' AND token_hash IS NOT NULL AND client_id IS NULL"
        " AND audience IS NULL) OR (kind = 'oauth' AND token_hash IS NULL"
        " AND client_id IS NOT NULL AND audience IS NOT NULL)",
    )
    op.create_table(
        "oauth_code",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_identity_id", sa.BigInteger(), nullable=False),
        sa.Column("scopes", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("grantee", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("code_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_oauth_code_code_hash")),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_oauth_code_farm_id_farm")),
        sa.ForeignKeyConstraint(
            ["created_by_identity_id"],
            ["local_identity.id"],
            name=op.f("fk_oauth_code_created_by_identity_id_local_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["data_grant.id"], name=op.f("fk_oauth_code_grant_id_data_grant")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_code")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_oauth_code_code_hash")),
    )
    for column in ("farm_id", "created_by_identity_id", "grant_id"):
        op.create_index(op.f(f"ix_oauth_code_{column}"), "oauth_code", [column])
    op.create_table(
        "oauth_token",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("grant_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_oauth_token_token_hash")),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name=op.f("ck_oauth_token_kind")),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["data_grant.id"], name=op.f("fk_oauth_token_grant_id_data_grant")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_oauth_token_token_hash")),
    )
    op.create_index(op.f("ix_oauth_token_grant_id"), "oauth_token", ["grant_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_token_grant_id"), table_name="oauth_token")
    op.drop_table("oauth_token")
    for column in ("grant_id", "created_by_identity_id", "farm_id"):
        op.drop_index(op.f(f"ix_oauth_code_{column}"), table_name="oauth_code")
    op.drop_table("oauth_code")
    # An OAuth connection's grant must not outlive its token as a plain grant.
    op.execute(
        "UPDATE data_grant SET revoked_at = now() WHERE revoked_at IS NULL AND id IN "
        "(SELECT grant_id FROM access_token WHERE kind = 'oauth')"
    )
    op.execute("DELETE FROM access_token WHERE kind = 'oauth'")
    op.drop_constraint(op.f("ck_access_token_kind_fields"), "access_token", type_="check")
    op.drop_constraint(op.f("ck_access_token_kind"), "access_token", type_="check")
    op.create_check_constraint(op.f("ck_access_token_kind"), "access_token", "kind IN ('personal')")
    op.alter_column("access_token", "token_hash", existing_type=sa.String(64), nullable=False)
    op.drop_column("access_token", "audience")
    op.drop_column("access_token", "client_id")
