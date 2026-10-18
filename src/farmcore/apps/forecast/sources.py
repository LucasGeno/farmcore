"""Forecast sources behind one small adapter: `runs()` returns the issued runs, each with its
issue time and hourly values under the station's variable names.

- `FileSource`: a saved file in the Open-Meteo hourly shape (`hourly.time` in UTC,
  `precipitation` = mm in the preceding hour, `wind_speed_10m` km/h, `temperature_2m` °C,
  `relative_humidity_2m` %), one entry per run under `runs` with its `issued_at`. The tests use a
  committed synthetic file; a downloaded file goes through the same reader.

- `SingleRunsDir`: a folder of Open-Meteo Single Runs API responses, model `ecmwf_ifs` (ECMWF
  IFS HRES 9 km), one file per run as `farmcore forecast fetch` saves it (the run time, the
  request URL and the raw response). The only source verified to
  return 2024 runs as issued. Read offline: no network here.

A source says whether its runs are the forecast as it was issued (`as_issued`); the page labels
a run that is not. Null values (the first step's rain and gusts) are missing, never zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

# Open-Meteo hourly name -> station variable (stations app, `HourlyObs` fields).
VARIABLES = {
    "precipitation": "rr1_mm",
    "wind_speed_10m": "ff_kmh",
    "temperature_2m": "t_c",
    "relative_humidity_2m": "rh_pct",
    "wind_gusts_10m": "fx_kmh",  # information only: the app has no gust rule
}
SINGLE_RUNS = "open-meteo-single-runs"
SINGLE_RUNS_MODEL = "ecmwf_ifs"
SINGLE_RUNS_LABEL = "ECMWF IFS HRES via Open-Meteo"
# ECMWF dissemination schedule: the 00Z/12Z control forecast is out to 360 h by run + 7 h 34 min
# (06:27/07:34 UTC for 00Z); taken as the run's availability, conservatively, since Open-Meteo's
# own lag in 2024 is not known.
ECMWF_AVAILABLE_AFTER = timedelta(hours=7, minutes=34)
ATTRIBUTION = (
    "Forecast: ECMWF IFS HRES (CC BY 4.0) via Open-Meteo.com (CC BY 4.0), aggregated to daily "
    "values"
)
UNITS = {
    "precipitation": "mm",
    "wind_speed_10m": "km/h",
    "wind_gusts_10m": "km/h",
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
}


@dataclass(frozen=True)
class Run:
    source: str
    issued_at: datetime
    label: str
    as_issued: bool
    latitude: float | None
    longitude: float | None
    values: list[tuple[datetime, str, float]]  # (ts UTC, variable, value)
    available_at: datetime | None = None  # when users could first read it; None: the run time
    props: dict[str, Any] = field(default_factory=dict)  # model, units, request URL, …

    @property
    def available(self) -> datetime:
        return self.available_at or self.issued_at


class ForecastSource(Protocol):
    def runs(self) -> list[Run]: ...


def _utc(text: str) -> datetime:
    """`2024-04-08T00:00` or `...Z` as an aware UTC datetime."""
    ts = datetime.fromisoformat(text.removesuffix("Z"))
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def parse_hourly(hourly: dict[str, Any]) -> list[tuple[datetime, str, float]]:
    """The values of one Open-Meteo `hourly` block; nulls are left out (a missing value)."""
    times = [_utc(t) for t in hourly["time"]]
    out = []
    for name, variable in VARIABLES.items():
        for ts, value in zip(times, hourly.get(name) or [], strict=False):
            if value is not None:
                out.append((ts, variable, float(value)))
    return out


class FileSource:
    def __init__(self, path: Path) -> None:
        self.path = path

    def runs(self) -> list[Run]:
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            Run(
                source=doc["source"],
                issued_at=_utc(run["issued_at"]),
                label=doc["label"],
                as_issued=bool(doc["as_issued"]),
                latitude=doc.get("latitude"),
                longitude=doc.get("longitude"),
                values=parse_hourly(run["hourly"]),
                available_at=_utc(run["available_at"]) if run.get("available_at") else None,
            )
            for run in doc["runs"]
        ]


class SingleRunsDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def runs(self) -> list[Run]:
        out = [self._run(p) for p in sorted(self.path.glob("*.json"))]
        return sorted(out, key=lambda r: r.issued_at)

    def _run(self, path: Path) -> Run:
        doc = json.loads(path.read_text(encoding="utf-8"))
        response = doc["response"]
        units = response.get("hourly_units") or {}
        for name, unit in UNITS.items():
            if name in units and units[name] != unit:
                raise ValueError(f"{path.name}: {name} is in {units[name]}, expected {unit}")
        issued = _utc(doc["run"])
        model = doc.get("model", SINGLE_RUNS_MODEL)
        return Run(
            source=f"{SINGLE_RUNS}/{model}",
            issued_at=issued,
            label=SINGLE_RUNS_LABEL,
            as_issued=True,
            latitude=response.get("latitude"),
            longitude=response.get("longitude"),
            values=parse_hourly(response["hourly"]),
            available_at=issued + ECMWF_AVAILABLE_AFTER,
            props={
                "model": model,
                "units": units,
                "request_url": doc.get("request_url"),
                "attribution": ATTRIBUTION,
            },
        )


# --- fetch (CLI only: the one network step) -------------------------------------------------

SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
RUNS_DIR = Path("open-meteo") / SINGLE_RUNS_MODEL  # under DATA_DIR, like meteofrance/
RUN_HOURS = (0, 12)
FORECAST_DAYS = 6


def single_run_url(run: datetime, latitude: float, longitude: float) -> str:
    hourly = ",".join(
        (
            "precipitation",
            "temperature_2m",
            "relative_humidity_2m",
            "wind_speed_10m",
            "wind_gusts_10m",
        )
    )
    return (
        f"{SINGLE_RUNS_URL}?latitude={latitude}&longitude={longitude}&hourly={hourly}"
        f"&models={SINGLE_RUNS_MODEL}&run={run:%Y-%m-%dT%H:%M}&forecast_days={FORECAST_DAYS}"
        "&timezone=UTC"
    )


def get_json(url: str) -> dict[str, Any]:
    import httpx

    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def fetch_single_runs(
    first: date, last: date, out: Path, latitude: float, longitude: float
) -> tuple[int, int]:
    """Save the 00Z and 12Z runs of every day in [first, last] to `out` (one file per run);
    a run already saved is skipped. Returns (saved, skipped)."""
    out.mkdir(parents=True, exist_ok=True)
    saved = skipped = 0
    day = first
    while day <= last:
        for hour in RUN_HOURS:
            run = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            path = out / f"run-{run:%Y-%m-%dT%H}.json"
            if path.exists():
                skipped += 1
                continue
            url = single_run_url(run, latitude, longitude)
            doc = {
                "source": "open-meteo single-runs",
                "model": SINGLE_RUNS_MODEL,
                "run": f"{run:%Y-%m-%dT%H:%M}",
                "request_url": url,
                "response": get_json(url),
            }
            path.write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")
            saved += 1
        day += timedelta(days=1)
    return saved, skipped
