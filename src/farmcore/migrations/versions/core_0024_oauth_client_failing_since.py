"""core: when a client's metadata URL first failed for good

Adds the nullable `oauth_client.failing_since`: when the client ID metadata document of a
`metadata` row first failed permanently (404, 410, a non-public address) since its last valid
fetch. Its connections end once that is older than `MCP_OAUTH_CIMD_GRACE`. Additive; downgrade
drops the column (a client then starts a new grace at its next failure).

Revision ID: core_0024
Revises: core_0023
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "core_0024"
down_revision: str | None = "core_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_client", sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("oauth_client", "failing_since")
