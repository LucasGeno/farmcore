"""planning: planned jobs, their parcels and append-only revision snapshots

Adds `(id, farm_id)` unique targets on advice, parcel and parameter_version so every planning
relation is a farm-scoped composite foreign key. Revision rows refuse UPDATE (trigger); DELETE
stays possible for demo reset.

Revision ID: planning_0001
Revises: forecast_0002
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "planning_0001"
down_revision: str | None = "forecast_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPED_TARGETS = ("advice", "parcel", "parameter_version")


def upgrade() -> None:
    for table in SCOPED_TARGETS:
        op.create_unique_constraint(op.f(f"uq_{table}_id_farm_id"), table, ["id", "farm_id"])
    op.create_table(
        "planning_job",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="planned", nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("assessment_id", sa.BigInteger(), nullable=False),
        sa.Column("config_version_id", sa.BigInteger(), nullable=True),
        sa.Column("config_version_status", sa.Text(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cancelled_by", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'cancelled')", name=op.f("ck_planning_job_status")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_planning_job_revision")),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 200", name=op.f("ck_planning_job_name")
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 1 AND 1440", name=op.f("ck_planning_job_duration_minutes")
        ),
        sa.CheckConstraint("length(notes) <= 10000", name=op.f("ck_planning_job_notes")),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0", name=op.f("ck_planning_job_created_by")
        ),
        sa.CheckConstraint(
            "length(btrim(updated_by)) > 0", name=op.f("ck_planning_job_updated_by")
        ),
        sa.CheckConstraint(
            "length(btrim(config_version_status)) > 0",
            name=op.f("ck_planning_job_config_version_status"),
        ),
        sa.CheckConstraint("jsonb_typeof(props) = 'object'", name=op.f("ck_planning_job_props")),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) "
            "OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)",
            name=op.f("ck_planning_job_cancel_metadata"),
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_planning_job_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id", "farm_id"],
            ["advice.id", "advice.farm_id"],
            name=op.f("fk_planning_job_assessment_id_advice"),
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id", "farm_id"],
            ["parameter_version.id", "parameter_version.farm_id"],
            name=op.f("fk_planning_job_config_version_id_parameter_version"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], name=op.f("fk_planning_job_product_id_product")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_job")),
        sa.UniqueConstraint("id", "farm_id", name=op.f("uq_planning_job_id_farm_id")),
    )
    op.create_index(
        "ix_planning_job_farm_start", "planning_job", ["farm_id", "scheduled_start", "id"]
    )
    op.create_index("ix_planning_job_assessment", "planning_job", ["assessment_id"])
    op.create_table(
        "planning_job_parcel",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("parcel_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id", "farm_id"],
            ["planning_job.id", "planning_job.farm_id"],
            name=op.f("fk_planning_job_parcel_job_id_planning_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parcel_id", "farm_id"],
            ["parcel.id", "parcel.farm_id"],
            name=op.f("fk_planning_job_parcel_parcel_id_parcel"),
        ),
        sa.PrimaryKeyConstraint("job_id", "parcel_id", name=op.f("pk_planning_job_parcel")),
    )
    op.create_index("ix_planning_job_parcel_parcel", "planning_job_parcel", ["parcel_id", "job_id"])
    op.create_table(
        "planning_job_revision",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_planning_job_revision_revision")),
        sa.CheckConstraint(
            "action IN ('created', 'edited', 'cancelled')",
            name=op.f("ck_planning_job_revision_action"),
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'cancelled')", name=op.f("ck_planning_job_revision_status")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name=op.f("ck_planning_job_revision_snapshot")
        ),
        sa.CheckConstraint("length(btrim(actor)) > 0", name=op.f("ck_planning_job_revision_actor")),
        sa.CheckConstraint("length(reason) <= 2000", name=op.f("ck_planning_job_revision_reason")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_planning_job_revision_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "farm_id"],
            ["planning_job.id", "planning_job.farm_id"],
            name=op.f("fk_planning_job_revision_job_id_planning_job"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_job_revision")),
        sa.UniqueConstraint(
            "job_id", "revision", name=op.f("uq_planning_job_revision_job_id_revision")
        ),
    )
    op.create_index(
        "ix_planning_job_revision_farm_job",
        "planning_job_revision",
        ["farm_id", "job_id", sa.literal_column("revision DESC")],
    )
    op.execute("""
        CREATE FUNCTION farmcore_planning_revision_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23000',
                MESSAGE = 'Plan revision history is append-only';
        END;
        $$
    """)
    op.execute(
        "CREATE TRIGGER planning_job_revision_immutable BEFORE UPDATE ON planning_job_revision "
        "FOR EACH ROW EXECUTE FUNCTION farmcore_planning_revision_immutable()"
    )


def downgrade() -> None:
    op.drop_index("ix_planning_job_revision_farm_job", table_name="planning_job_revision")
    op.drop_table("planning_job_revision")
    op.execute("DROP FUNCTION farmcore_planning_revision_immutable()")
    op.drop_index("ix_planning_job_parcel_parcel", table_name="planning_job_parcel")
    op.drop_table("planning_job_parcel")
    op.drop_index("ix_planning_job_assessment", table_name="planning_job")
    op.drop_index("ix_planning_job_farm_start", table_name="planning_job")
    op.drop_table("planning_job")
    for table in reversed(SCOPED_TARGETS):
        op.drop_constraint(op.f(f"uq_{table}_id_farm_id"), table, type_="unique")
