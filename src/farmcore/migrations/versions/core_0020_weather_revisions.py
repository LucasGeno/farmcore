"""core: provisional weather observations and per-farm ERA5 grid stations

ERA5T (the newest ERA5 days) is revised by the final ERA5 about two months later. Observations
gain `provisional` and `revision_note`, and the unique key takes `provisional` in, so a final
value is appended beside the provisional one (never written over it): an as-of read of an
earlier time still reads what was known then.

Backfill (no value is rewritten):
- An ERA5 (`copernicus-era5`) value is provisional unless its month ended 3 or more months
  before its receipt was fetched: a period end from the start of the month 3 months before the
  fetch's month (UTC) on, the rule of `weather_era5.provider.provisional_from`. Every other
  provider's values stay final.
- ERA5 grid stations become per farm: each public grid station's values move to a station of
  the farm whose receipt holds them (`farm{id}:{lat},{lon}`, owned by that farm), and a public
  grid station left with no values is deleted. Values held through no farm's receipt stay.

Downgrade returns every value to the old shape or refuses, naming counts, while the old key
cannot hold it: a final value stored beside a provisional one, or two farms holding the same
hour of one grid cell. Nothing is deleted to get past a refusal.

Revision ID: core_0020
Revises: core_0019
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from farmcore.core.migration_refusal import MigrationRefused

revision: str = "core_0020"
down_revision: str | None = "core_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ERA5 = "copernicus-era5"
UNIQUE = "uq_weather_observation_station_period"
KEY = ["station_id", "variable", "period_start", "period_end"]
FINAL_AFTER_MONTHS = 3  # weather_era5.provider.FINAL_AFTER_MONTHS when this revision was written


def upgrade() -> None:
    op.add_column(
        "weather_observation",
        sa.Column("provisional", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("weather_observation", sa.Column("revision_note", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE weather_observation o SET provisional = true FROM weather_receipt r "
            "WHERE r.id = o.receipt_id AND r.provider = :era5 AND o.period_end >= "
            "(date_trunc('month', r.fetched_at AT TIME ZONE 'UTC') - make_interval(months => :m)) "
            "AT TIME ZONE 'UTC'"
        ).bindparams(era5=ERA5, m=FINAL_AFTER_MONTHS)
    )
    op.drop_constraint(UNIQUE, "weather_observation", type_="unique")
    op.create_unique_constraint(UNIQUE, "weather_observation", [*KEY, "provisional"])

    op.execute(
        sa.text(
            "INSERT INTO weather_station "
            "(provider, provider_station_id, name, geom, elevation_m, owner_farm_id) "
            "SELECT s.provider, 'farm' || p.farm_id || ':' || s.provider_station_id, s.name, "
            "s.geom, s.elevation_m, p.farm_id "
            "FROM (SELECT DISTINCT o.station_id, r.farm_id FROM weather_observation o "
            "JOIN weather_receipt r ON r.id = o.receipt_id WHERE r.farm_id IS NOT NULL) p "
            "JOIN weather_station s ON s.id = p.station_id "
            "WHERE s.provider = :era5 AND s.owner_farm_id IS NULL"
        ).bindparams(era5=ERA5)
    )
    op.execute(
        sa.text(
            "UPDATE weather_observation o SET station_id = f.id "
            "FROM weather_receipt r, weather_station s, weather_station f "
            "WHERE r.id = o.receipt_id AND s.id = o.station_id "
            "AND s.provider = :era5 AND s.owner_farm_id IS NULL "
            "AND f.provider = s.provider AND f.owner_farm_id = r.farm_id "
            "AND f.provider_station_id = 'farm' || r.farm_id || ':' || s.provider_station_id"
        ).bindparams(era5=ERA5)
    )
    op.execute(
        sa.text(
            "DELETE FROM weather_station s WHERE s.provider = :era5 AND s.owner_farm_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM weather_observation o WHERE o.station_id = s.id)"
        ).bindparams(era5=ERA5)
    )


def _count(bind: sa.Connection, sql: str) -> int:
    return bind.execute(sa.text(sql), {"era5": ERA5} if ":era5" in sql else {}).scalar_one()


PER_FARM = (
    "FROM weather_station f WHERE f.provider = :era5 AND f.owner_farm_id IS NOT NULL "
    "AND f.provider_station_id = 'farm' || f.owner_farm_id || ':' || "
    "split_part(f.provider_station_id, ':', 2)"
)


def downgrade() -> None:
    bind = op.get_bind()
    revised = _count(
        bind,
        "SELECT count(*) FROM weather_observation p JOIN weather_observation f "
        "USING (station_id, variable, period_start, period_end) "
        "WHERE p.provisional AND NOT f.provisional",
    )
    if revised:
        raise MigrationRefused(
            f"core_0020 downgrade: {revised} provisional observation(s) have a final value "
            "stored beside them; the old key holds one value per station, variable and period"
        )
    shared = _count(
        bind,
        "SELECT count(*) FROM (SELECT o.variable, o.period_start, o.period_end, "
        "split_part(f.provider_station_id, ':', 2) AS cell FROM weather_observation o "
        f"JOIN (SELECT f.id, f.provider_station_id {PER_FARM}) f ON f.id = o.station_id "
        "GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) d",
    )
    if shared:
        raise MigrationRefused(
            f"core_0020 downgrade: {shared} hour(s) of an ERA5 grid cell are held by more than "
            "one farm; the old shared grid station holds one value per hour"
        )
    op.execute(
        sa.text(
            "INSERT INTO weather_station (provider, provider_station_id, name, geom, elevation_m) "
            "SELECT DISTINCT ON (split_part(f.provider_station_id, ':', 2)) f.provider, "
            f"split_part(f.provider_station_id, ':', 2), f.name, f.geom, f.elevation_m {PER_FARM} "
            "ORDER BY split_part(f.provider_station_id, ':', 2), f.id "
            "ON CONFLICT (provider, provider_station_id) DO NOTHING"
        ).bindparams(era5=ERA5)
    )
    op.execute(
        sa.text(
            "UPDATE weather_observation o SET station_id = s.id "
            f"FROM (SELECT f.id, f.provider_station_id {PER_FARM}) f, weather_station s "
            "WHERE o.station_id = f.id AND s.provider = :era5 "
            "AND s.provider_station_id = split_part(f.provider_station_id, ':', 2)"
        ).bindparams(era5=ERA5)
    )
    op.execute(
        sa.text(f"DELETE FROM weather_station WHERE id IN (SELECT f.id {PER_FARM})").bindparams(
            era5=ERA5
        )
    )
    op.drop_constraint(UNIQUE, "weather_observation", type_="unique")
    op.create_unique_constraint(UNIQUE, "weather_observation", KEY)
    op.drop_column("weather_observation", "revision_note")
    op.drop_column("weather_observation", "provisional")
