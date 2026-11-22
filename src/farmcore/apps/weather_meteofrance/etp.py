"""Météo-France's official daily ET0 grid for a farm, from its data.gouv.fr files.

Dataset: "Données ETP spatialisées quotidiennes", Météo-France,
https://www.data.gouv.fr/datasets/donnees-etp-spatialisees-quotidiennes (read 2026-11-23): daily
Penman-Monteith potential evapotranspiration interpolated on a 0.125 degree grid over metropolitan
France; one gzip CSV per year (`QUOT_ETPgrille_<year>.csv.gz`, updated daily for the current
year) with the columns `longitude;latitude;date;ETP_grille` (WGS 84 degrees, `AAAAMMJJ`, mm).
Licence Ouverte / Etalab 2.0, which asks for the source and the date of its last update: both
are in the receipt's attribution.

`import_file` keeps the grid cell whose centre is nearest the farm pin, if the pin lies in it (a
pin outside the grid is refused), and stores each day's value as `variables.ET0` over the UTC
day `[D, D+1)` (`assumed`: the files do not state the day's bounds) under the farm's own
station for that cell (owned by the farm, so private; two farms in one cell never share one, as
ERA5's grid points). The receipt is tier `national`; its payload hash is the cell's rows, so a
file re-imported with the same values for that cell is the same receipt. An empty value is
missing (no row stored, never zero); a value that is not a finite number of 0 mm or more refuses
the whole file before anything is written.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.orm import Session

from farmcore.core.farm_location import farm_location
from farmcore.core.models.canonical import Farm
from farmcore.core.weather.contract import (
    Fetched,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    Tier,
)
from farmcore.core.weather.selection import great_circle_m
from farmcore.core.weather.store import record_receipt, store_observations, upsert_station
from farmcore.core.weather.variables import ET0, VARIABLES

PROVIDER = "meteofrance-etp"
DATASET = "ETP_QUOT/QUOT_ETPgrille"
SOURCE_URL = "https://www.data.gouv.fr/datasets/donnees-etp-spatialisees-quotidiennes"
LICENCE_ID = "etalab-2.0"
HALF_CELL = 0.0625  # degrees: half the grid step
COLUMNS = ("longitude", "latitude", "date", "ETP_grille")


class EtpFileError(ValueError):
    """A file that cannot be imported as given; nothing is stored."""


@dataclass(frozen=True)
class Imported:
    station: str
    days: int
    receipt_id: int


def licence(last_update: date) -> Licence:
    return Licence(
        LICENCE_ID,
        "Météo-France, Données ETP spatialisées quotidiennes (data.gouv.fr), "
        f"Licence Ouverte / Etalab 2.0, last updated {last_update.isoformat()}",
        True,
    )


@contextmanager
def _open(path: Path) -> Iterator[io.TextIOBase]:
    """The file as text, gunzipped when it is gzip (the published form)."""
    with path.open("rb") as raw:
        gzipped = raw.read(2) == b"\x1f\x8b"
        raw.seek(0)
        with gzip.open(raw) if gzipped else nullcontext(raw) as stream:
            yield io.TextIOWrapper(stream, encoding="utf-8", newline="")


def cell_rows(path: Path, pin: Point) -> tuple[Point, list[tuple[date, Decimal]], bytes]:
    """The rows of the cell containing `pin`: its centre, its (day, mm) values and the rows
    as read (the receipt's payload)."""
    candidates: dict[tuple[str, str], list[list[str]]] = {}
    with _open(path) as text:
        reader = csv.reader(text, delimiter=";")
        header = [c.strip() for c in next(reader, [])]
        if tuple(header) != COLUMNS:
            raise EtpFileError(f"expected columns {';'.join(COLUMNS)}, got {';'.join(header)}")
        for line, row in enumerate(reader, start=2):
            if len(row) != len(COLUMNS):
                raise EtpFileError(f"line {line}: expected {len(COLUMNS)} fields")
            try:
                lon, lat = float(row[0]), float(row[1])
            except ValueError:
                raise EtpFileError(f"line {line}: coordinates are not numbers") from None
            near = HALF_CELL * 2
            if abs(lat - pin.latitude) <= near and abs(lon - pin.longitude) <= near:
                candidates.setdefault((row[0], row[1]), []).append([line, *row])
    inside = [
        key
        for key in candidates
        if abs(float(key[1]) - pin.latitude) <= HALF_CELL
        and abs(float(key[0]) - pin.longitude) <= HALF_CELL
    ]
    if not inside:
        raise EtpFileError("no grid cell of this file contains the farm pin")
    key = min(inside, key=lambda k: (great_circle_m(pin, Point(float(k[1]), float(k[0]))), k))
    values: list[tuple[date, Decimal]] = []
    for line, _, _, day, value in candidates[key]:
        try:
            when = datetime.strptime(day.strip(), "%Y%m%d").date()
        except ValueError:
            raise EtpFileError(f"line {line}: date {day!r} is not AAAAMMJJ") from None
        if not value.strip():
            continue  # missing: never zero
        try:
            number = Decimal(value.strip().replace(",", "."))
        except InvalidOperation:
            raise EtpFileError(f"line {line}: ETP {value!r} is not a number") from None
        if not number.is_finite() or number < 0:
            raise EtpFileError(f"line {line}: ETP {value!r} is not a finite number of mm >= 0")
        values.append((when, number))
    payload = "\n".join(";".join(r[1:]) for r in candidates[key]).encode()
    return Point(float(key[1]), float(key[0])), values, payload


def import_file(
    session: Session,
    farm_id: int,
    path: Path,
    *,
    last_update: date,
    url: str | None = None,
    fetched_at: datetime | None = None,
) -> Imported:
    """Store the farm's cell of one ETP grid file (see the module docstring)."""
    pin = farm_location(session.get(Farm, farm_id))
    if pin is None:
        raise EtpFileError("the farm has no location")
    centre, values, payload = cell_rows(path, Point(pin["latitude"], pin["longitude"]))
    station = Station(
        provider_station_id=f"farm{farm_id}:{centre.latitude:.3f},{centre.longitude:.3f}",
        name=f"Météo-France ETP grid cell {centre.latitude:.3f}, {centre.longitude:.3f}",
        latitude=centre.latitude,
        longitude=centre.longitude,
    )
    unit = VARIABLES[ET0].unit
    observations = [
        ObservationValue(
            ET0,
            unit,
            mm,
            datetime(day.year, day.month, day.day, tzinfo=UTC),
            datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1),
        )
        for day, mm in values
    ]
    fetched = Fetched(
        request=Request(
            dataset=DATASET,
            url=url,
            params={"file": path.name, "last_update": last_update.isoformat(), "page": SOURCE_URL},
        ),
        fetched_at=fetched_at or datetime.now(UTC),
        payload=payload,
        values=observations,
    )
    receipt = record_receipt(
        session,
        provider=PROVIDER,
        licence=licence(last_update),
        fetched=fetched,
        farm_id=farm_id,
        tier=Tier.NATIONAL,
    )
    row = upsert_station(session, PROVIDER, station, owner_farm_id=farm_id)
    store_observations(session, receipt, row, observations)
    return Imported(station.provider_station_id, len(observations), receipt.id)
