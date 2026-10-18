"""Synthetic Météo-France station CSVs (made-up values, the real file's columns)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

HEADER = "num_poste,nom_usuel,lat,lon,alti_m,timestamp_utc,rr1_mm,t_c,rh_pct,ff_ms,ff_kmh,fxi_ms"
FILE = "meteofrance/demo_station_33394002_2024_2025.csv"

Row = tuple[datetime, str, str, str, str]


def write_csv(data_dir: Path, rows: list[Row]) -> Path:
    """rows: (ts UTC, rr1_mm, t_c, rh_pct, ff_kmh) as CSV text; '' is a null cell."""
    path = data_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [HEADER]
    for ts, rr1, t, rh, ff in rows:
        stamp = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"33394002,ST EMILION,44.917667,-0.188,38,{stamp},{rr1},{t},{rh},,{ff},")
    path.write_text("\n".join(lines) + "\n")
    return path


def hourly(start: datetime, hours: int) -> list[Row]:
    """`hours` complete synthetic rows from `start`."""
    return [
        (start + timedelta(hours=i), "0.2", f"{10 + i % 5}.0", "80", "7.0") for i in range(hours)
    ]
