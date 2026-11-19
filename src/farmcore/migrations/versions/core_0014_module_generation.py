"""core: a module-state generation and module settings on `app_module`

Adds `app_module.generation` (bigint, 0) and `app_module.settings` (jsonb, `{}`), and a trigger
that adds one to `generation` whenever a row's `state` changes, whoever changes it (`/apps`,
`farmcore apps`, plain SQL). A durable task's attempt keeps the generation it began in and may
publish only while its module is still installed in that generation (`core/tasks.py`), so a
disable, or a disable and re-enable, fences work begun before it. Additive: existing rows keep
their state at generation 0 with no settings, and a downgrade drops the trigger and both columns.

Revision ID: core_0014
Revises: core_0013
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0014"
down_revision: str | None = "core_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_module",
        sa.Column("generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "app_module",
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        "CREATE FUNCTION app_module_next_generation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN NEW.generation := OLD.generation + 1; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER app_module_state_generation BEFORE UPDATE OF state ON app_module "
        "FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state) "
        "EXECUTE FUNCTION app_module_next_generation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER app_module_state_generation ON app_module")
    op.execute("DROP FUNCTION app_module_next_generation()")
    op.drop_column("app_module", "settings")
    op.drop_column("app_module", "generation")
