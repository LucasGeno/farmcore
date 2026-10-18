"""canonical: every core farm table (farm, parcel, asset, product, log, quantity, observation,
source, log_source, ext_id, data_grant, access_log, advice, event, llm_audit)

Revision ID: core_0002
Revises: core_0001
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision: str = "core_0002"
down_revision: str | None = "core_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ext_id",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("res_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ext_id")),
        sa.UniqueConstraint("source", "key", name=op.f("uq_ext_id_source_key")),
    )
    op.create_table(
        "farm",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm")),
    )
    op.create_table(
        "llm_audit",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_audit")),
    )
    op.create_table(
        "product",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("authorisation_no", sa.Text(), nullable=False),
        sa.Column("active_substance", sa.Text(), nullable=False),
        sa.Column("product_type", sa.Text(), nullable=False),
        sa.Column(
            "targets", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "product_type IN ('contact', 'penetrant')", name=op.f("ck_product_product_type")
        ),
        sa.CheckConstraint(
            "targets <@ ARRAY['downy', 'powdery']::text[]", name=op.f("ck_product_targets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product")),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("licence", sa.Text(), nullable=False),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source")),
        sa.UniqueConstraint("code", name=op.f("uq_source_code")),
    )
    op.create_geospatial_table(
        "asset",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('station', 'sprayer', 'regional')", name=op.f("ck_asset_kind")
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_asset_farm_id_farm")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset")),
    )
    op.create_geospatial_index(
        "ix_asset_geom", "asset", ["geom"], unique=False, postgresql_using="gist", postgresql_ops={}
    )
    op.create_table(
        "data_grant",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("grantee", sa.Text(), nullable=False),
        sa.Column("scopes", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_data_grant_farm_id_farm")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_grant")),
    )
    op.create_table(
        "event",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("res_model", sa.Text(), nullable=False),
        sa.Column("res_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("source_app", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_event_farm_id_farm")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
    )
    op.create_index(
        "ix_event_res_model_res_id_occurred_at",
        "event",
        ["res_model", "res_id", sa.literal_column("occurred_at DESC")],
        unique=False,
    )
    op.create_table(
        "log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ts_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ts_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bbch", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('pending', 'done')", name=op.f("ck_log_status")),
        sa.CheckConstraint(
            "type IN ('spray', 'fertilise', 'irrigate', 'harvest', 'observation')",
            name=op.f("ck_log_type"),
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_log_farm_id_farm")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_log")),
    )
    op.create_index(
        "ix_log_farm_id_type_ts_start", "log", ["farm_id", "type", "ts_start"], unique=False
    )
    op.create_geospatial_table(
        "parcel",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("iacs_id", sa.Text(), nullable=True),
        sa.Column("area_ha", sa.Numeric(), nullable=False),
        sa.Column(
            "geom",
            Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_parcel_farm_id_farm")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parcel")),
        sa.UniqueConstraint("farm_id", "alias", name=op.f("uq_parcel_farm_id_alias")),
    )
    op.create_geospatial_index(
        "ix_parcel_geom",
        "parcel",
        ["geom"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_table(
        "access_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("grant_id", sa.BigInteger(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_access_log_farm_id_farm")),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["data_grant.id"], name=op.f("fk_access_log_grant_id_data_grant")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_log")),
    )
    op.create_index("ix_access_log_farm_id_ts", "access_log", ["farm_id", "ts"], unique=False)
    op.create_table(
        "advice",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("parcel_id", sa.BigInteger(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("next_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("protected_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "products",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('spray', 'wait', 'protected', 'none')", name=op.f("ck_advice_decision")
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farm.id"], name=op.f("fk_advice_farm_id_farm")),
        sa.ForeignKeyConstraint(
            ["parcel_id"], ["parcel.id"], name=op.f("fk_advice_parcel_id_parcel")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_advice")),
    )
    op.create_index(
        "ix_advice_parcel_id_evaluated_at",
        "advice",
        ["parcel_id", sa.literal_column("evaluated_at DESC")],
        unique=False,
    )
    op.create_table(
        "log_parcel",
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("parcel_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["log_id"], ["log.id"], name=op.f("fk_log_parcel_log_id_log"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parcel_id"], ["parcel.id"], name=op.f("fk_log_parcel_parcel_id_parcel")
        ),
        sa.PrimaryKeyConstraint("log_id", "parcel_id", name=op.f("pk_log_parcel")),
    )
    op.create_table(
        "log_source",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("precision_rank", sa.Integer(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_type IN ('voice', 'seed', 'isoxml', 'file')",
            name=op.f("ck_log_source_source_type"),
        ),
        sa.ForeignKeyConstraint(
            ["log_id"], ["log.id"], name=op.f("fk_log_source_log_id_log"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_log_source")),
    )
    op.create_table(
        "observation",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("asset_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("variable", sa.Text(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name=op.f("fk_observation_asset_id_asset")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observation")),
        sa.UniqueConstraint(
            "asset_id", "ts", "variable", name=op.f("uq_observation_asset_id_ts_variable")
        ),
    )
    op.create_index(
        "ix_observation_ts", "observation", ["ts"], unique=False, postgresql_using="brin"
    )
    op.create_table(
        "quantity",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "measure IN ('rate', 'volume', 'area')", name=op.f("ck_quantity_measure")
        ),
        sa.ForeignKeyConstraint(
            ["log_id"], ["log.id"], name=op.f("fk_quantity_log_id_log"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], name=op.f("fk_quantity_product_id_product")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quantity")),
    )


def downgrade() -> None:
    op.drop_table("quantity")
    op.drop_index("ix_observation_ts", table_name="observation", postgresql_using="brin")
    op.drop_table("observation")
    op.drop_table("log_source")
    op.drop_table("log_parcel")
    op.drop_index("ix_advice_parcel_id_evaluated_at", table_name="advice")
    op.drop_table("advice")
    op.drop_index("ix_access_log_farm_id_ts", table_name="access_log")
    op.drop_table("access_log")
    op.drop_geospatial_index(
        "ix_parcel_geom", table_name="parcel", postgresql_using="gist", column_name="geom"
    )
    op.drop_geospatial_table("parcel")
    op.drop_index("ix_log_farm_id_type_ts_start", table_name="log")
    op.drop_table("log")
    op.drop_index("ix_event_res_model_res_id_occurred_at", table_name="event")
    op.drop_table("event")
    op.drop_table("data_grant")
    op.drop_geospatial_index(
        "ix_asset_geom", table_name="asset", postgresql_using="gist", column_name="geom"
    )
    op.drop_geospatial_table("asset")
    op.drop_table("source")
    op.drop_table("product")
    op.drop_table("llm_audit")
    op.drop_table("farm")
    op.drop_table("ext_id")
