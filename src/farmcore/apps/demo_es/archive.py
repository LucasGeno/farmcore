"""Source-native half-hour labels; never assign an unverified UTC or civil timezone."""

import csv
from datetime import datetime, timedelta
from pathlib import Path

FILE = "inforiego/VA07_2024_03_10_halfhourly.csv"
NOTICE = "Advisor withheld: InfoRiego timestamp basis unverified. Archive only, not live weather."
VARIABLES = (
    ("Precipitación (mm)", "Rain", "mm"),
    ("Temperatura (ºC)", "Temperature", "°C"),
    ("Humedad relativa (%)", "Humidity", "%"),
    ("Radiación (W/m2)", "Radiation", "W/m²"),
    ("Vel. viento (m/s)", "Wind", "m/s"),
    ("Dir. viento (º)", "Direction", "°"),
)


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter=";"))
    if not rows or any(row["Código"] != "VA07" for row in rows):
        raise ValueError("Expected the single-station InfoRiego VA07 archive")
    return rows


def source_time(row: dict[str, str]) -> datetime:
    day = datetime.fromisoformat(row["Fecha (AAAA-MM-DD)"])
    hour, minute = divmod(int(row["Hora (HHMM)"]), 100)
    if hour > 24 or minute >= 60 or (hour == 24 and minute):
        raise ValueError("Invalid source HHMM label")
    return day + timedelta(hours=hour, minutes=minute)


def summary(rows: list[dict[str, str]]) -> dict:
    times = sorted({source_time(row) for row in rows})
    if not times:
        return {
            "rows": 0,
            "duplicates": 0,
            "first": "",
            "last": "",
            "missing_slots": 0,
            "gaps": [],
            "coverage_pct": 0.0,
            "time_basis": "unverified",
            "notice": NOTICE,
        }
    gaps = []
    for a, b in zip(times[:-1], times[1:], strict=True):
        missing = int((b - a) / timedelta(minutes=30)) - 1
        if missing > 0:
            gaps.append({"after": str(a)[:16], "before": str(b)[:16], "missing": missing})
    missing = sum(g["missing"] for g in gaps)
    return {
        "rows": len(rows),
        "duplicates": len(rows) - len(times),
        "first": str(times[0])[:16],
        "last": str(times[-1])[:16],
        "missing_slots": missing,
        "gaps": gaps,
        "coverage_pct": round(100 * len(times) / (len(times) + missing), 2),
        "time_basis": "unverified",
        "notice": NOTICE,
    }
