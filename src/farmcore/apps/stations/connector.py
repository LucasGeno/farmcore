"""Météo-France replay connector: the demo station CSV -> `observation` rows.

Station 33394002 (St-Émilion), hourly, Licence Ouverte. Variables `rr1_mm`, `t_c`, `rh_pct`,
`ff_kmh`; a null cell gives no row (never 0). Rows upsert on (asset_id, ts, variable).
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from farmcore.core.models.canonical import Observation

EXT_SOURCE = "meteofrance"
STATION_KEY = "33394002"
SOURCE_CODE = "meteofrance-33394002"
FILE = "meteofrance/demo_station_33394002_2024_2025.csv"
VARIABLES = ("rr1_mm", "t_c", "rh_pct", "ff_kmh")
# Replay ingests observed hours up to clock now + 72 h as the "forecast" (the
# advisor's next window looks 72 h ahead).
REPLAY_HORIZON = timedelta(hours=72)
_CHUNK = 5000  # rows per INSERT (4 bind params each, well under PostgreSQL's 65535)


@dataclass(frozen=True)
class Hour:
    ts: datetime  # UTC, aware
    values: dict[str, float | None]  # every variable; None = null cell


def _number(cell: str) -> float | None:
    """A CSV cell as a float; empty, "nan" or "inf" is None (missing, never a number)."""
    if not cell.strip():
        return None
    value = float(cell)
    return value if math.isfinite(value) else None


def read_hours(path: Path) -> list[Hour]:
    """Every row of the station CSV, ordered by time; empty cells are None."""
    with path.open(newline="", encoding="utf-8") as f:
        hours = [
            Hour(
                ts=datetime.fromisoformat(row["timestamp_utc"]),
                values={v: _number(row[v]) for v in VARIABLES},
            )
            for row in csv.DictReader(f)
        ]
    hours.sort(key=lambda h: h.ts)
    return hours


def observations(hours: Iterable[Hour]) -> list[tuple[datetime, str, float]]:
    """(ts, variable, value) for every non-null cell."""
    return [(h.ts, v, x) for h in hours for v, x in h.values.items() if x is not None]


def upsert(session: Session, asset_id: int, rows: list[tuple[datetime, str, float]]) -> None:
    """Insert or update `rows` for `asset_id` (`ON CONFLICT (asset_id, ts, variable) DO UPDATE`)."""
    for i in range(0, len(rows), _CHUNK):
        stmt = insert(Observation).values(
            [
                {"asset_id": asset_id, "ts": ts, "variable": var, "value": value}
                for ts, var, value in rows[i : i + _CHUNK]
            ]
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["asset_id", "ts", "variable"],
                set_={"value": stmt.excluded.value},
            )
        )
