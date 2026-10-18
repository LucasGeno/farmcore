"""Advisor test helpers: the St-Émilion demo station series (open Météo-France data) as `HourlyObs`.

Tests that use `station_series` must be marked `data`; the fixture skips when the file is absent.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from farmcore.apps.advisor.engine import HourlyObs
from tests.conftest import DATA_DIR

STATION_CSV = DATA_DIR / "meteofrance" / "demo_station_33394002_2024_2025.csv"


def _num(raw: str | None) -> float | None:
    return float(raw) if raw not in (None, "") else None


def load_station_csv(path: Path) -> list[HourlyObs]:
    """Rows of the cleaned demo station CSV; empty cells become `None` (never 0)."""
    with path.open(newline="") as fh:
        return [
            HourlyObs(
                ts=datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00")),
                rr1_mm=_num(row["rr1_mm"]),
                t_c=_num(row["t_c"]),
                rh_pct=_num(row["rh_pct"]),
                ff_kmh=_num(row["ff_kmh"]),
            )
            for row in csv.DictReader(fh)
        ]


@pytest.fixture(scope="session")
def station_series() -> list[HourlyObs]:
    if not STATION_CSV.is_file():
        pytest.skip(f"{STATION_CSV.name} is absent from {DATA_DIR}")
    return load_station_csv(STATION_CSV)
