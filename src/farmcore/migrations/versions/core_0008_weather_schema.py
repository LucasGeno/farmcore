"""core: normalised weather schema beside the station and forecast tables (expand)

Adds the frozen `weather_variable` table (each CF standard name FarmCore stores, the unit its
values are stored in and the legacy station/forecast variable it maps; a parity test keeps it
equal to `farmcore.core.weather.variables`) and the normalised tables every weather provider
writes: `weather_receipt` (provider, dataset, keyless request, fetch time, licence, attribution,
redistribution flag, payload hash, library version), `weather_station` (WIGOS ID, provider ID,
location, elevation, owner), `weather_observation` and `weather_forecast` (CF variable, decimal
value in that variable's unit, the period it covers, receipt; a forecast's period is null when
the provider does not say).

A receipt written without a licence reads `unknown` and is not redistributable; the database
refuses an unknown licence (`unknown`, `noassertion` or `none`, in any case or padding) marked
redistributable, whoever writes it.

Purely additive: `observation`, `forecast_run` and `forecast_value` stay the only source every
current consumer reads. Downgrade drops the new tables.

Revision ID: core_0008
Revises: core_0007
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0008"
down_revision: str | None = "core_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: CF standard name -> (stored unit, legacy variable). CF Standard Name Table version 95
# (2026-09-16); each unit converts to the name's canonical unit (UDUNITS).
VARIABLES = {
    "lwe_thickness_of_precipitation_amount": ("mm", "rr1_mm"),
    "air_temperature": ("degC", "t_c"),
    "relative_humidity": ("%", "rh_pct"),
    "wind_speed": ("km h-1", "ff_kmh"),
    "wind_speed_of_gust": ("km h-1", "fx_kmh"),
}
WIGOS_ID = "^[0-9]{1,5}-[0-9]{1,5}-[0-9]{1,5}-[0-9A-Za-z]{1,16}$"
FORECAST_PERIOD = "(period_start IS NULL AND period_end IS NULL) OR period_end >= period_start"
QC_FLAGS = "qc_flag IN ('unchecked', 'passed', 'suspect', 'failed')"


def _variable(table: str) -> list[sa.Column | sa.Constraint]:
    return [
        sa.Column("variable", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.CheckConstraint("value <> 'NaN'::numeric", name=op.f(f"ck_{table}_value")),
        sa.ForeignKeyConstraint(
            ["variable", "unit"],
            ["weather_variable.name", "weather_variable.unit"],
            name=op.f(f"fk_{table}_variable_weather_variable"),
        ),
    ]


def upgrade() -> None:
    weather_variable = op.create_table(
        "weather_variable",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("legacy_name", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_weather_variable")),
        sa.UniqueConstraint("name", "unit", name=op.f("uq_weather_variable_name_unit")),
        sa.UniqueConstraint("legacy_name", name=op.f("uq_weather_variable_legacy_name")),
    )
    op.bulk_insert(
        weather_variable,
        [
            {"name": name, "unit": unit, "legacy_name": legacy}
            for name, (unit, legacy) in VARIABLES.items()
        ],
    )
    op.create_table(
        "weather_receipt",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("licence", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column("redistribution", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("library_version", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(btrim(licence)) BETWEEN 1 AND 100", name=op.f("ck_weather_receipt_licence")
        ),
        sa.CheckConstraint(
            "lower(btrim(licence)) NOT IN ('unknown', 'noassertion', 'none') OR NOT redistribution",
            name=op.f("ck_weather_receipt_unknown_not_redistributed"),
        ),
        sa.CheckConstraint(
            "payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_weather_receipt_payload_hash"),
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farm.id"], name=op.f("fk_weather_receipt_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weather_receipt")),
    )
    op.create_table(
        "weather_station",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_station_id", sa.Text(), nullable=False),
        sa.Column("wigos_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="POINT", srid=4326, spatial_index=False, from_text="ST_GeomFromEWKT"
            ),
            nullable=False,
        ),
        sa.Column("elevation_m", sa.Numeric(), nullable=True),
        sa.Column("owner_farm_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            f"wigos_id IS NULL OR wigos_id ~ '{WIGOS_ID}'",
            name=op.f("ck_weather_station_wigos_id"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_farm_id"], ["farm.id"], name=op.f("fk_weather_station_owner_farm_id_farm")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weather_station")),
        sa.UniqueConstraint(
            "provider",
            "provider_station_id",
            name=op.f("uq_weather_station_provider_provider_station_id"),
        ),
        sa.UniqueConstraint("wigos_id", name=op.f("uq_weather_station_wigos_id")),
    )
    op.create_index("ix_weather_station_geom", "weather_station", ["geom"], postgresql_using="gist")
    op.create_table(
        "weather_observation",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("station_id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qc_flag", sa.Text(), server_default="unchecked", nullable=False),
        *_variable("weather_observation"),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_weather_observation_period")
        ),
        sa.CheckConstraint(QC_FLAGS, name=op.f("ck_weather_observation_qc_flag")),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["weather_station.id"],
            name=op.f("fk_weather_observation_station_id_weather_station"),
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["weather_receipt.id"],
            name=op.f("fk_weather_observation_receipt_id_weather_receipt"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weather_observation")),
        sa.UniqueConstraint(
            "station_id",
            "variable",
            "period_start",
            "period_end",
            name="uq_weather_observation_station_period",
        ),
    )
    op.create_index(
        op.f("ix_weather_observation_receipt_id"), "weather_observation", ["receipt_id"]
    )
    op.create_table(
        "weather_forecast",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("member", sa.Integer(), server_default="0", nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        *_variable("weather_forecast"),
        sa.CheckConstraint("member >= 0", name=op.f("ck_weather_forecast_member")),
        sa.CheckConstraint(FORECAST_PERIOD, name=op.f("ck_weather_forecast_period")),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["weather_receipt.id"],
            name=op.f("fk_weather_forecast_receipt_id_weather_receipt"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weather_forecast")),
        sa.UniqueConstraint(
            "receipt_id",
            "model",
            "valid_time",
            "member",
            "variable",
            name="uq_weather_forecast_receipt_value",
        ),
    )


def downgrade() -> None:
    op.drop_table("weather_forecast")
    op.drop_index(op.f("ix_weather_observation_receipt_id"), table_name="weather_observation")
    op.drop_table("weather_observation")
    op.drop_index("ix_weather_station_geom", table_name="weather_station", postgresql_using="gist")
    op.drop_table("weather_station")
    op.drop_table("weather_receipt")
    op.drop_table("weather_variable")
