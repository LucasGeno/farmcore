"""Immutable scoped parameter versions, reviews and activations.

Revision ID: review_0001
Revises: operations_0002
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "review_0001"
down_revision: str | None = "operations_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "parameter_version",
        sa.Column("parent_version_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parameter_catalog", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("baseline", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("crop", sa.Text(), server_default="vine", nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("evaluation_year", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_parameter_version_config_hash")
        ),
        sa.CheckConstraint("crop = 'vine'", name=op.f("ck_parameter_version_crop")),
        sa.CheckConstraint(
            "jsonb_typeof(effective_config) = 'object'",
            name=op.f("ck_parameter_version_effective_config"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(overrides) = 'object'", name=op.f("ck_parameter_version_overrides")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameter_catalog) = 'object'",
            name=op.f("ck_parameter_version_parameter_catalog"),
        ),
        sa.CheckConstraint(
            "evaluation_year BETWEEN 1900 AND 2200",
            name=op.f("ck_parameter_version_evaluation_year"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0", name=op.f("ck_parameter_version_created_by")
        ),
        sa.CheckConstraint(
            "length(btrim(evidence)) > 0", name=op.f("ck_parameter_version_evidence")
        ),
        sa.CheckConstraint("length(btrim(region)) > 0", name=op.f("ck_parameter_version_region")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_parameter_version_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id", "farm_id", "crop", "region", "evaluation_year"],
            [
                "parameter_version.id",
                "parameter_version.farm_id",
                "parameter_version.crop",
                "parameter_version.region",
                "parameter_version.evaluation_year",
            ],
            name=op.f("fk_parameter_version_parent_version_id_parameter_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parameter_version")),
        sa.UniqueConstraint(
            "id",
            "farm_id",
            "crop",
            "region",
            "evaluation_year",
            name=op.f("uq_parameter_version_id_farm_id_crop_region_evaluation_year"),
        ),
    )
    op.create_index(
        "ix_parameter_version_baseline_scope",
        "parameter_version",
        ["farm_id", "crop", "region", "evaluation_year"],
        unique=True,
        postgresql_where=sa.text("baseline"),
    )
    op.create_index(
        "ix_parameter_version_scope_id",
        "parameter_version",
        ["farm_id", "crop", "region", "evaluation_year", sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_table(
        "parameter_review",
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("crop", sa.Text(), server_default="vine", nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("evaluation_year", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("crop = 'vine'", name=op.f("ck_parameter_review_crop")),
        sa.CheckConstraint(
            "verdict IN ('approved', 'rejected', 'unsure')",
            name=op.f("ck_parameter_review_verdict"),
        ),
        sa.CheckConstraint(
            "evaluation_year BETWEEN 1900 AND 2200",
            name=op.f("ck_parameter_review_evaluation_year"),
        ),
        sa.CheckConstraint(
            "length(btrim(evidence)) > 0", name=op.f("ck_parameter_review_evidence")
        ),
        sa.CheckConstraint("length(btrim(note)) > 0", name=op.f("ck_parameter_review_note")),
        sa.CheckConstraint("length(btrim(region)) > 0", name=op.f("ck_parameter_review_region")),
        sa.CheckConstraint(
            "length(btrim(reviewer)) > 0", name=op.f("ck_parameter_review_reviewer")
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_parameter_review_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "farm_id", "crop", "region", "evaluation_year"],
            [
                "parameter_version.id",
                "parameter_version.farm_id",
                "parameter_version.crop",
                "parameter_version.region",
                "parameter_version.evaluation_year",
            ],
            name=op.f("fk_parameter_review_version_id_parameter_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parameter_review")),
        sa.UniqueConstraint(
            "id",
            "version_id",
            "farm_id",
            "crop",
            "region",
            "evaluation_year",
            name=op.f("uq_parameter_review_id_version_id_farm_id_crop_region_evaluation_year"),
        ),
    )
    op.create_index(
        "ix_parameter_review_version_id",
        "parameter_review",
        ["version_id", sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_table(
        "parameter_activation",
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("review_id", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("crop", sa.Text(), server_default="vine", nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("evaluation_year", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("crop = 'vine'", name=op.f("ck_parameter_activation_crop")),
        sa.CheckConstraint(
            "evaluation_year BETWEEN 1900 AND 2200",
            name=op.f("ck_parameter_activation_evaluation_year"),
        ),
        sa.CheckConstraint("length(btrim(actor)) > 0", name=op.f("ck_parameter_activation_actor")),
        sa.CheckConstraint(
            "length(btrim(region)) > 0", name=op.f("ck_parameter_activation_region")
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_parameter_activation_farm_id_farm")
        ),
        sa.ForeignKeyConstraint(
            ["review_id", "version_id", "farm_id", "crop", "region", "evaluation_year"],
            [
                "parameter_review.id",
                "parameter_review.version_id",
                "parameter_review.farm_id",
                "parameter_review.crop",
                "parameter_review.region",
                "parameter_review.evaluation_year",
            ],
            name=op.f("fk_parameter_activation_review_id_parameter_review"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "farm_id", "crop", "region", "evaluation_year"],
            [
                "parameter_version.id",
                "parameter_version.farm_id",
                "parameter_version.crop",
                "parameter_version.region",
                "parameter_version.evaluation_year",
            ],
            name=op.f("fk_parameter_activation_version_id_parameter_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parameter_activation")),
    )
    op.create_index(
        "ix_parameter_activation_scope_id",
        "parameter_activation",
        ["farm_id", "crop", "region", "evaluation_year", sa.literal_column("id DESC")],
        unique=False,
    )
    op.execute("""
        CREATE FUNCTION farmcore_parameter_history_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23000',
                MESSAGE = 'Parameter history is append-only';
        END;
        $$
    """)
    for table in ("parameter_version", "parameter_review", "parameter_activation"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION farmcore_parameter_history_immutable()"
        )


def downgrade() -> None:
    op.drop_index("ix_parameter_activation_scope_id", table_name="parameter_activation")
    op.drop_table("parameter_activation")
    op.drop_index("ix_parameter_review_version_id", table_name="parameter_review")
    op.drop_table("parameter_review")
    op.drop_index("ix_parameter_version_scope_id", table_name="parameter_version")
    op.drop_index(
        "ix_parameter_version_baseline_scope",
        table_name="parameter_version",
        postgresql_where=sa.text("baseline"),
    )
    op.drop_table("parameter_version")
    op.execute("DROP FUNCTION farmcore_parameter_history_immutable()")
