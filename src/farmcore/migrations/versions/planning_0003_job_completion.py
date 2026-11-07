"""planning: complete a planned job into one confirmed canonical operation

A planned job may end `completed`, linked through a farm-scoped composite foreign key to the
confirmed `log` it produced, with who/when and the client idempotency key. Completion and
cancellation metadata follow one rule: present exactly when the job is in that terminal status.
Revision rows record the `completed` action, and every revision action implies its status.
`log_source` gains the `planning` provenance for farmer-confirmed planned work.

Downgrade refuses while completed jobs, completed revisions or `planning` log sources exist:
the earlier schema cannot represent them and deleting confirmed history is not a schema step.

Revision ID: planning_0003
Revises: planning_0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "planning_0003"
down_revision: str | None = "planning_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "status IN ('planned', 'cancelled')"
NEW_STATUSES = "status IN ('planned', 'cancelled', 'completed')"
OLD_ACTIONS = "action IN ('created', 'edited', 'cancelled')"
NEW_ACTIONS = "action IN ('created', 'edited', 'cancelled', 'completed')"
OLD_SOURCES = "source_type IN ('voice', 'seed', 'isoxml', 'file')"
NEW_SOURCES = "source_type IN ('voice', 'seed', 'isoxml', 'file', 'planning')"
COMPLETION_COLUMNS = ("completed_by", "completed_at", "operation_log_id", "operation_request_key")


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    # Farm-scoped target for the completion's canonical operation.
    op.create_unique_constraint(op.f("uq_log_id_farm_id"), "log", ["id", "farm_id"])

    _replace_check("planning_job", "status", NEW_STATUSES)
    op.add_column("planning_job", sa.Column("completed_by", sa.Text(), nullable=True))
    op.add_column(
        "planning_job", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("planning_job", sa.Column("operation_log_id", sa.BigInteger(), nullable=True))
    op.add_column("planning_job", sa.Column("operation_request_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_planning_job_cancelled_by"),
        "planning_job",
        "cancelled_by IS NULL OR length(btrim(cancelled_by)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_planning_job_completed_by"),
        "planning_job",
        "completed_by IS NULL OR length(btrim(completed_by)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_planning_job_operation_request_key"),
        "planning_job",
        "operation_request_key IS NULL OR length(btrim(operation_request_key)) BETWEEN 1 AND 128",
    )
    op.create_check_constraint(
        op.f("ck_planning_job_complete_metadata"),
        "planning_job",
        "(status = 'completed' AND completed_at IS NOT NULL AND completed_by IS NOT NULL "
        "AND operation_log_id IS NOT NULL AND operation_request_key IS NOT NULL) "
        "OR (status <> 'completed' AND completed_at IS NULL AND completed_by IS NULL "
        "AND operation_log_id IS NULL AND operation_request_key IS NULL)",
    )
    op.create_foreign_key(
        op.f("fk_planning_job_operation_log_id_log"),
        "planning_job",
        "log",
        ["operation_log_id", "farm_id"],
        ["id", "farm_id"],
    )
    op.create_index(
        "ix_planning_job_operation_log",
        "planning_job",
        ["operation_log_id"],
        postgresql_where=sa.text("operation_log_id IS NOT NULL"),
    )
    # One idempotency key per farm: a retried double tap cannot become a second completion.
    op.create_index(
        "ix_planning_job_farm_request_key",
        "planning_job",
        ["farm_id", "operation_request_key"],
        unique=True,
        postgresql_where=sa.text("operation_request_key IS NOT NULL"),
    )

    _replace_check("planning_job_revision", "action", NEW_ACTIONS)
    _replace_check("planning_job_revision", "status", NEW_STATUSES)
    # Creation and edits snapshot a planned job; a terminal action snapshots its own status.
    op.create_check_constraint(
        op.f("ck_planning_job_revision_action_status"),
        "planning_job_revision",
        "(action IN ('created', 'edited') AND status = 'planned') "
        "OR (action IN ('cancelled', 'completed') AND status = action)",
    )

    _replace_check("log_source", "source_type", NEW_SOURCES)


def downgrade() -> None:
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM planning_job WHERE status = 'completed') "
            "+ (SELECT count(*) FROM planning_job_revision "
            "   WHERE status = 'completed' OR action = 'completed') "
            "+ (SELECT count(*) FROM log_source WHERE source_type = 'planning')"
        )
    ).scalar_one()
    if blocking:
        raise RuntimeError(
            "planning_0003 downgrade refused: completed planned jobs, their revisions or "
            "'planning' log sources exist and planning_0002 cannot represent them"
        )

    _replace_check("log_source", "source_type", OLD_SOURCES)

    op.drop_constraint(
        op.f("ck_planning_job_revision_action_status"), "planning_job_revision", type_="check"
    )
    _replace_check("planning_job_revision", "status", OLD_STATUSES)
    _replace_check("planning_job_revision", "action", OLD_ACTIONS)

    op.drop_index("ix_planning_job_farm_request_key", table_name="planning_job")
    op.drop_index("ix_planning_job_operation_log", table_name="planning_job")
    op.drop_constraint(
        op.f("fk_planning_job_operation_log_id_log"), "planning_job", type_="foreignkey"
    )
    for name in ("complete_metadata", "operation_request_key", "completed_by", "cancelled_by"):
        op.drop_constraint(op.f(f"ck_planning_job_{name}"), "planning_job", type_="check")
    for column in reversed(COMPLETION_COLUMNS):
        op.drop_column("planning_job", column)
    _replace_check("planning_job", "status", OLD_STATUSES)

    op.drop_constraint(op.f("uq_log_id_farm_id"), "log", type_="unique")
