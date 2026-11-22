"""core: soil moisture as a stored weather variable

Adds the frozen `weather_variable` row for soil moisture: CF standard name
`volume_fraction_of_condensed_water_in_soil` (CF table 95, canonical unit 1), stored in `%`, no
legacy variable. On-farm soil probes push it (`weather_push`); the depth it is read at is the
probe's station, so no column changes.

Downgrade removes the row, or refuses (counts only) while any observation or forecast value
holds it. Nothing is deleted to get past a refusal.

Revision ID: core_0021
Revises: core_0020
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from farmcore.core.migration_refusal import MigrationRefused

revision: str = "core_0021"
down_revision: str | None = "core_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "volume_fraction_of_condensed_water_in_soil"  # variables.SOIL_MOISTURE when written
UNIT = "%"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO weather_variable (name, unit, legacy_name) VALUES (:name, :unit, NULL)"
        ).bindparams(name=NAME, unit=UNIT)
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE variable = :name"), {"name": NAME}
        ).scalar_one()
        for table in ("weather_observation", "weather_forecast")
    }
    if any(held.values()):
        counts = ", ".join(f"{n} in {t}" for t, n in held.items() if n)
        raise MigrationRefused(f"core_0021 downgrade: soil moisture values are held ({counts})")
    op.execute(sa.text("DELETE FROM weather_variable WHERE name = :name").bindparams(name=NAME))
