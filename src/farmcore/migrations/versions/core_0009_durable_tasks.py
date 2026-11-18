"""core: durable background tasks (Procrastinate 3.10.0) and their publication fence

Applies Procrastinate's schema (its tables `procrastinate_*`, enum types, functions and
triggers, all in `public`) from a vendored copy whose SHA-256 is checked first, so this revision
never changes when the package is upgraded: a later library schema change gets its own revision
from the library's migration files. Procrastinate's code is MIT (THIRD_PARTY_NOTICES.md).

Adds FarmCore's own fence, independent of the library's job status: `task_attempt` (one row per
task and farm, its current attempt number, the clock generation it started in, its deadline) and
`clock_state.generation`, bumped by every live/replay switch and by restore.

Downgrade drops FarmCore's fence, then the library's functions and procedures (with its
triggers), its tables and its types: the library ships no "drop schema".

Revision ID: core_0009
Revises: catalog_fr_0001
Create Date: 2026-11-18
"""

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "core_0009"
down_revision: str | None = "catalog_fr_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = Path(__file__).resolve().parents[1] / "vendor" / "procrastinate_3_10_0_schema.sql"
SCHEMA_SHA256 = "c70ec4b400a60ad9592787653aae5ae77ae41bf801d56b5bd2712751a07f2009"
TABLES = (
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
    "procrastinate_workers",
)
TYPES = (
    "procrastinate_job_to_defer_v1",
    "procrastinate_job_event_type",
    "procrastinate_job_status",
)


def upgrade() -> None:
    body = SCHEMA.read_bytes()
    if sha256(body).hexdigest() != SCHEMA_SHA256:
        raise RuntimeError(f"{SCHEMA.name} does not match the schema this revision was written for")
    # The driver's own execute: several statements, `%` in RAISE messages, no bind parameters.
    op.get_bind().connection.driver_connection.execute(body.decode())

    op.add_column(
        "clock_state",
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "task_attempt",
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("clock_generation", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["farm_id"],
            ["farm.id"],
            name=op.f("fk_task_attempt_farm_id_farm"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_name", "farm_id", name=op.f("pk_task_attempt")),
    )


def downgrade() -> None:
    op.drop_table("task_attempt")
    op.drop_column("clock_state", "generation")
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT p.oid::regprocedure AS sig, p.prokind FROM pg_proc p
                     WHERE p.pronamespace = 'public'::regnamespace
                       AND p.proname LIKE 'procrastinate\\_%'
            LOOP
                IF r.prokind = 'p' THEN
                    EXECUTE 'DROP PROCEDURE ' || r.sig;
                ELSE  -- CASCADE: only the library's own triggers call its trigger functions
                    EXECUTE 'DROP FUNCTION ' || r.sig || ' CASCADE';
                END IF;
            END LOOP;
        END $$
        """
    )
    op.execute(f"DROP TABLE {', '.join(TABLES)}")
    op.execute(f"DROP TYPE {', '.join(TYPES)}")
