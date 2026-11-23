"""core: global radiation and reference evapotranspiration as stored weather variables

Adds two frozen `weather_variable` rows (CF table 95), neither replacing a legacy variable:

- `surface_downwelling_shortwave_flux_in_air` in `W m-2` (global radiation, a period mean), the
  FAO-56 radiation input Open-Meteo forecasts;
- `water_potential_evapotranspiration_amount` in `kg m-2` (1 kg m-2 of water is 1 mm), the
  official daily ET0 Météo-France publishes on its grid.

Downgrade removes the rows, or refuses (counts only) while any observation or forecast value
holds one. Nothing is deleted to get past a refusal.

Revision ID: core_0022
Revises: core_0021
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from farmcore.core.migration_refusal import MigrationRefused

revision: str = "core_0022"
down_revision: str | None = "core_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# variables.SHORTWAVE and variables.ET0 when written
ROWS = (
    ("surface_downwelling_shortwave_flux_in_air", "W m-2"),
    ("water_potential_evapotranspiration_amount", "kg m-2"),
)


def upgrade() -> None:
    for name, unit in ROWS:
        op.execute(
            sa.text(
                "INSERT INTO weather_variable (name, unit, legacy_name) VALUES (:name, :unit, NULL)"
            ).bindparams(name=name, unit=unit)
        )


def downgrade() -> None:
    bind = op.get_bind()
    names = [name for name, _ in ROWS]
    held = {
        table: bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE variable = ANY(:names)"),
            {"names": names},
        ).scalar_one()
        for table in ("weather_observation", "weather_forecast")
    }
    if any(held.values()):
        counts = ", ".join(f"{n} in {t}" for t, n in held.items() if n)
        raise MigrationRefused(f"core_0022 downgrade: radiation or ET0 values are held ({counts})")
    op.execute(
        sa.text("DELETE FROM weather_variable WHERE name = ANY(:names)").bindparams(names=names)
    )
