"""KPI test data (invented values): a synthetic station CSV shaped like the open Météo-France
demo file, and a demo farm with the station, two parcels (25.59 ha) and the replay clock."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from farmcore.apps.stations import seed as stations_seed
from farmcore.apps.stations.connector import FILE
from farmcore.core import seed as core_seed
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Parcel

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
NOW = datetime(2024, 5, 1, 4, 0, tzinfo=UTC)  # 1 May 2024 06:00 Paris


def seed_farm(
    session, settings, *, station: bool = True, areas=("12.5", "13.09")
) -> tuple[Farm, list[Parcel]]:
    """The demo farm with (optionally) station 33394002, parcels of `areas` ha, clock at NOW."""
    farm = core_seed.ensure_default_farm(session, settings)
    if station:
        stations_seed.seed(session, scene=None, data_dir=settings.data_dir)
    parcels = [
        Parcel(farm_id=farm.id, name=f"P{i}", alias=f"P{i}", area_ha=Decimal(a), geom=SQUARE)
        for i, a in enumerate(areas, 1)
    ]
    session.add_all(parcels)
    set_replay(session, NOW, settings)
    session.flush()
    return farm, parcels


COLUMNS = ("timestamp_utc", "rr1_mm", "t_c", "rh_pct", "ff_kmh")
# Wet hours (UTC) that start a 3-10 event in each season: 12 mm, 15 °C.
RAIN = {datetime(2024, 5, 6, h, tzinfo=UTC): 2.0 for h in range(6, 12)} | {
    datetime(2025, 5, 12, h, tzinfo=UTC): 2.0 for h in range(6, 12)
}


def write_station(data_dir: Path, years: tuple[int, ...] = (2024, 2025)) -> Path:
    """Hourly rows 1 Apr – 31 Jul of each year under `data_dir / FILE`; returns the path."""
    path = data_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for year in years:
            ts = datetime.combine(date(year, 4, 1), datetime.min.time(), tzinfo=UTC)
            end = datetime(year, 8, 1, tzinfo=UTC)
            while ts < end:
                rain = RAIN.get(ts, 0.0)
                w.writerow(
                    (
                        ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        rain,
                        15.0,
                        95 if rain else 60,
                        5.0,
                    )
                )
                ts += timedelta(hours=1)
    return path
