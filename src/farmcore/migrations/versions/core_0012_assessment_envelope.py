"""core: a shared assessment evidence envelope, and crop protection's advice linked to it

Adds `assessment`: one module's typed result (`kind`, `kind_version`, `result`) with its evidence
(farm, optional parcel of that farm, evaluation time, status, configuration version or
configuration, input snapshot). A trigger refuses every UPDATE, so saved evidence is never
rewritten: it is immutable, deletable only with its advice (whose foreign key holds it) or by a
demo reset of its farm.

Adds `advice.assessment_id` (unique, nullable): advice written from now on points at its
envelope, which holds its input snapshot and unversioned configuration instead of `props`. The
composite foreign key makes the envelope the same farm, parcel and evaluation time as the advice.
Existing advice is not touched: it keeps its evidence in `props` and has no envelope.

Downgrade first copies each linked envelope's input snapshot and unversioned configuration back
into its advice's `props` (and its configuration version where `props` has none), so advice saved
after the upgrade stays reproducible on the previous release. Envelopes not linked to advice (no
other kind exists in this release) are dropped; their count is logged.

Revision ID: core_0012
Revises: core_0011
Create Date: 2026-11-19
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0012"
down_revision: str | None = "core_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.create_table(
        "assessment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("parcel_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("kind_version", sa.Integer(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("config_version", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('evaluable', 'limited', 'unavailable')", name=op.f("ck_assessment_status")
        ),
        sa.CheckConstraint(
            r"kind ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'", name=op.f("ck_assessment_kind")
        ),
        sa.CheckConstraint("kind_version >= 1", name=op.f("ck_assessment_kind_version")),
        sa.CheckConstraint(
            "jsonb_typeof(config_version) = 'object'", name=op.f("ck_assessment_config_version")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object'", name=op.f("ck_assessment_input_snapshot")
        ),
        sa.CheckConstraint("jsonb_typeof(result) = 'object'", name=op.f("ck_assessment_result")),
        sa.CheckConstraint(
            "effective_config IS NULL OR jsonb_typeof(effective_config) = 'object'",
            name=op.f("ck_assessment_effective_config"),
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_assessment_farm_id_farm")),
        sa.ForeignKeyConstraint(
            ["parcel_id", "farm_id"],
            ["parcel.id", "parcel.farm_id"],
            name=op.f("fk_assessment_parcel_id_parcel"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessment")),
        sa.UniqueConstraint(
            "id",
            "farm_id",
            "parcel_id",
            "evaluated_at",
            name=op.f("uq_assessment_id_farm_id_parcel_id_evaluated_at"),
        ),
    )
    op.create_index(
        "ix_assessment_farm_id_kind_evaluated_at",
        "assessment",
        ["farm_id", "kind", "evaluated_at"],
        unique=False,
    )
    op.execute("""
        CREATE FUNCTION farmcore_assessment_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23000',
                MESSAGE = 'Assessment evidence is immutable';
        END;
        $$
    """)
    op.execute(
        "CREATE TRIGGER assessment_immutable BEFORE UPDATE ON assessment "
        "FOR EACH ROW EXECUTE FUNCTION farmcore_assessment_immutable()"
    )
    op.add_column("advice", sa.Column("assessment_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(op.f("uq_advice_assessment_id"), "advice", ["assessment_id"])
    op.create_foreign_key(
        op.f("fk_advice_assessment_id_assessment"),
        "advice",
        "assessment",
        ["assessment_id", "farm_id", "parcel_id", "evaluated_at"],
        ["id", "farm_id", "parcel_id", "evaluated_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    restored = bind.execute(
        sa.text("""
            UPDATE advice SET props = jsonb_build_object('config_version', a.config_version)
                || advice.props
                || jsonb_build_object('input_snapshot', a.input_snapshot)
                || CASE WHEN a.effective_config IS NULL THEN '{}'::jsonb
                        ELSE jsonb_build_object('effective_config', a.effective_config) END
            FROM assessment a
            WHERE advice.assessment_id = a.id
        """)
    ).rowcount
    unlinked = bind.execute(
        sa.text(
            "SELECT count(*) FROM assessment a "
            "WHERE NOT EXISTS (SELECT 1 FROM advice WHERE advice.assessment_id = a.id)"
        )
    ).scalar_one()
    logger.info(
        "core_0012 downgrade: evidence of %d advice rows moved back to props; "
        "%d assessments of other kinds dropped",
        restored,
        unlinked,
    )
    op.drop_constraint(op.f("fk_advice_assessment_id_assessment"), "advice", type_="foreignkey")
    op.drop_constraint(op.f("uq_advice_assessment_id"), "advice", type_="unique")
    op.drop_column("advice", "assessment_id")
    op.execute("DROP TRIGGER assessment_immutable ON assessment")
    op.execute("DROP FUNCTION farmcore_assessment_immutable()")
    op.drop_index("ix_assessment_farm_id_kind_evaluated_at", table_name="assessment")
    op.drop_table("assessment")
