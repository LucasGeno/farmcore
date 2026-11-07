"""planning: machines, operators, their availability windows and job assignments

A machine or operator booking is one `planning_job_assignment` row with a half-open
`[starts_at, ends_at)` window. Two partial GiST exclusion constraints reject overlapping
bookings of the same machine or operator atomically (SQLSTATE 23P01); they need `btree_gist`
for bigint equality. NULL capacity and the absence of availability windows mean unknown.

Revision ID: planning_0002
Revises: planning_0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "planning_0002"
down_revision: str | None = "planning_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCES = ("machine", "operator")


def _audit_columns() -> list[sa.Column]:
    return [
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
    ]


def _audit_checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint("length(btrim(created_by)) > 0", name=op.f(f"ck_{table}_created_by")),
        sa.CheckConstraint("length(btrim(updated_by)) > 0", name=op.f(f"ck_{table}_updated_by")),
    ]


def _resource_fks(table: str, ondelete: str | None) -> list[sa.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(
            [f"{kind}_id", "farm_id"],
            [f"planning_{kind}.id", f"planning_{kind}.farm_id"],
            name=op.f(f"fk_{table}_{kind}_id_planning_{kind}"),
            ondelete=ondelete,
        )
        for kind in RESOURCES
    ]


def upgrade() -> None:
    # Trusted extension (PostgreSQL 13+), shipped in the postgis/postgis image's contrib.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "planning_machine",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="sprayer", nullable=False),
        sa.Column("work_rate_ha_per_hour", sa.Numeric(8, 3), nullable=True),
        sa.Column("tank_litres", sa.Numeric(10, 2), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        *_audit_columns(),
        sa.CheckConstraint(
            "kind IN ('sprayer', 'tractor', 'other')", name=op.f("ck_planning_machine_kind")
        ),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 120", name=op.f("ck_planning_machine_name")
        ),
        sa.CheckConstraint(
            "work_rate_ha_per_hour IS NULL OR work_rate_ha_per_hour > 0",
            name=op.f("ck_planning_machine_work_rate"),
        ),
        sa.CheckConstraint(
            "tank_litres IS NULL OR tank_litres > 0", name=op.f("ck_planning_machine_tank_litres")
        ),
        sa.CheckConstraint("length(notes) <= 2000", name=op.f("ck_planning_machine_notes")),
        *_audit_checks("planning_machine"),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_planning_machine_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_machine")),
        sa.UniqueConstraint("id", "farm_id", name=op.f("uq_planning_machine_id_farm_id")),
        sa.UniqueConstraint("farm_id", "name", name=op.f("uq_planning_machine_farm_id_name")),
    )
    op.create_table(
        "planning_operator",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        *_audit_columns(),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 120", name=op.f("ck_planning_operator_name")
        ),
        sa.CheckConstraint("length(notes) <= 2000", name=op.f("ck_planning_operator_notes")),
        *_audit_checks("planning_operator"),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_planning_operator_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_operator")),
        sa.UniqueConstraint("id", "farm_id", name=op.f("uq_planning_operator_id_farm_id")),
        sa.UniqueConstraint("farm_id", "name", name=op.f("uq_planning_operator_farm_id_name")),
    )
    op.create_table(
        "planning_resource_window",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("machine_id", sa.BigInteger(), nullable=True),
        sa.Column("operator_id", sa.BigInteger(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        *_audit_columns(),
        sa.CheckConstraint(
            "num_nonnulls(machine_id, operator_id) = 1",
            name=op.f("ck_planning_resource_window_resource"),
        ),
        sa.CheckConstraint("starts_at < ends_at", name=op.f("ck_planning_resource_window_range")),
        sa.CheckConstraint("length(notes) <= 2000", name=op.f("ck_planning_resource_window_notes")),
        *_audit_checks("planning_resource_window"),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_planning_resource_window_farm_id_farm")
        ),
        *_resource_fks("planning_resource_window", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_resource_window")),
    )
    for kind in RESOURCES:
        op.create_index(
            f"ix_planning_resource_window_{kind}",
            "planning_resource_window",
            ["farm_id", f"{kind}_id", "starts_at", "ends_at"],
            postgresql_where=sa.text(f"{kind}_id IS NOT NULL"),
        )
    op.create_table(
        "planning_job_assignment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("machine_id", sa.BigInteger(), nullable=True),
        sa.Column("operator_id", sa.BigInteger(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
        sa.CheckConstraint(
            "num_nonnulls(machine_id, operator_id) = 1",
            name=op.f("ck_planning_job_assignment_resource"),
        ),
        sa.CheckConstraint("starts_at < ends_at", name=op.f("ck_planning_job_assignment_range")),
        *_audit_checks("planning_job_assignment"),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_planning_job_assignment_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "farm_id"],
            ["planning_job.id", "planning_job.farm_id"],
            name=op.f("fk_planning_job_assignment_job_id_planning_job"),
            ondelete="CASCADE",
        ),
        *_resource_fks("planning_job_assignment", None),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_job_assignment")),
    )
    for kind in RESOURCES:
        # The atomic overlap rejection: one live booking per resource per instant.
        op.execute(
            f"ALTER TABLE planning_job_assignment ADD CONSTRAINT ex_planning_job_assignment_{kind} "
            f"EXCLUDE USING gist (farm_id WITH =, {kind}_id WITH =, "
            "tstzrange(starts_at, ends_at, '[)') WITH &&) "
            f"WHERE ({kind}_id IS NOT NULL)"
        )
        # At most one machine and one operator per job.
        op.create_index(
            f"ix_planning_job_assignment_job_{kind}",
            "planning_job_assignment",
            ["job_id"],
            unique=True,
            postgresql_where=sa.text(f"{kind}_id IS NOT NULL"),
        )
    op.create_index("ix_planning_job_assignment_job", "planning_job_assignment", ["job_id"])
    op.create_index(
        "ix_planning_job_assignment_farm_start",
        "planning_job_assignment",
        ["farm_id", "starts_at", "ends_at"],
    )


def downgrade() -> None:
    op.drop_table("planning_job_assignment")  # its indexes and exclusion constraints go with it
    op.drop_table("planning_resource_window")
    op.drop_table("planning_operator")
    op.drop_table("planning_machine")
    # btree_gist is left in place, like postgis in core_0001: other objects may rely on it.
