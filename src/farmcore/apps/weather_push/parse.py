"""SensorThings-shaped readings, as JSON or CSV, into values the weather store holds.

JSON is a Thing with its Datastreams and their Observations inline (SensorThings API 1.1 deep
insert):

    {"name": "North block station",
     "Datastreams": [{"ObservedProperty": {"name": "air_temperature"},
                      "unitOfMeasurement": {"symbol": "degC"},
                      "properties": {"depth_m": 0.3},          # soil moisture only
                      "Observations": [{"phenomenonTime": "2026-11-22T10:00:00Z",
                                        "result": 12.5}]}]}

The observed property is a CF standard name (`name`, or the last path segment of
`definition`, e.g. the NERC vocabulary URL). CSV has one reading per row under the header
`observed_property,unit,phenomenon_time,result,depth_m` (`depth_m` may be left out).

Each reading must be one the schema can hold honestly, or the whole payload is refused
(`Refused`, nothing stored): a variable FarmCore stores (`variables.VARIABLES`), a unit this
module converts from (`UNITS`), an exact decimal within the variable's physical range, a time
with its zone and not in the future, a period that fits the variable (`time: point`: an
instant; `time: sum`: exactly one hour; `time: mean`/`maximum`: at most one hour) ending on
the hour (the merge layer is hourly), a depth for soil moisture and for nothing else, and no
reading given twice.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from farmcore.core.weather.contract import ObservationValue
from farmcore.core.weather.variables import SOIL_MOISTURE, VARIABLES

MAX_READINGS = 5000  # assumed: a day of 10-minute readings from a dozen datastreams, with room
FUTURE_SKEW = timedelta(minutes=5)  # assumed: a device clock running slightly fast
HOUR = timedelta(hours=1)
MAX_DEPTH_M = Decimal("10")  # assumed: deeper than any soil moisture probe
CSV_COLUMNS = ("observed_property", "unit", "phenomenon_time", "result", "depth_m")

_ONE, _ZERO = Decimal(1), Decimal(0)
# Per CF variable, each accepted unit symbol (UDUNITS, UCUM or the plain form) -> (factor,
# offset) into the stored unit: stored = value * factor + offset. Exact decimals throughout.
UNITS: dict[str, dict[str, tuple[Decimal, Decimal]]] = {
    "lwe_thickness_of_precipitation_amount": {"mm": (_ONE, _ZERO)},
    "air_temperature": {
        "degC": (_ONE, _ZERO),
        "°C": (_ONE, _ZERO),
        "Cel": (_ONE, _ZERO),
        "K": (_ONE, Decimal("-273.15")),
    },
    "relative_humidity": {"%": (_ONE, _ZERO)},
    **{
        name: {
            "km h-1": (_ONE, _ZERO),
            "km/h": (_ONE, _ZERO),
            "m s-1": (Decimal("3.6"), _ZERO),
            "m/s": (Decimal("3.6"), _ZERO),
        }
        for name in ("wind_speed", "wind_speed_of_gust")
    },
    SOIL_MOISTURE: {
        "%": (_ONE, _ZERO),
        "m3 m-3": (Decimal(100), _ZERO),
        "m3/m3": (Decimal(100), _ZERO),
        "1": (Decimal(100), _ZERO),
    },
}
# Physical bounds in the stored unit (inclusive; None: unbounded): a value outside is a unit
# mistake or a broken sensor, never a reading.
RANGES: dict[str, tuple[Decimal | None, Decimal | None]] = {
    "lwe_thickness_of_precipitation_amount": (_ZERO, None),
    "air_temperature": (Decimal("-273.15"), None),
    "relative_humidity": (_ZERO, Decimal(100)),
    "wind_speed": (_ZERO, None),
    "wind_speed_of_gust": (_ZERO, None),
    SOIL_MOISTURE: (_ZERO, Decimal(100)),
}


class Refused(ValueError):
    """A payload that cannot be stored as given; the message says which reading and why."""


@dataclass(frozen=True)
class Reading:
    value: ObservationValue
    depth_m: Decimal | None  # soil moisture only


def parse_json(body: bytes, now: datetime) -> list[Reading]:
    try:
        thing = json.loads(
            body, parse_float=Decimal, parse_int=Decimal, parse_constant=_no_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, Refused) as exc:
        raise Refused(f"not JSON: {exc}") from None
    streams = thing.get("Datastreams") if isinstance(thing, dict) else None
    if not isinstance(streams, list) or not streams:
        raise Refused("a Thing with its Datastreams is expected")
    rows = []
    for n, stream in enumerate(streams):
        where = f"Datastreams[{n}]"
        if not isinstance(stream, dict):
            raise Refused(f"{where}: not an object")
        prop, unit = stream.get("ObservedProperty"), stream.get("unitOfMeasurement")
        observations = stream.get("Observations")
        props = stream.get("properties") or {}
        if not isinstance(prop, dict) or not isinstance(unit, dict) or not isinstance(props, dict):
            raise Refused(f"{where}: ObservedProperty and unitOfMeasurement are objects")
        if not isinstance(observations, list):
            raise Refused(f"{where}: Observations is a list")
        name = prop.get("name") or str(prop.get("definition") or "").rstrip("/").rsplit("/")[-1]
        for m, obs in enumerate(observations):
            if not isinstance(obs, dict):
                raise Refused(f"{where}.Observations[{m}]: not an object")
            rows.append(
                (
                    f"{where}.Observations[{m}]",
                    name,
                    unit.get("symbol"),
                    obs.get("phenomenonTime"),
                    obs.get("result"),
                    props.get("depth_m"),
                )
            )
    return _readings(rows, now)


def parse_csv(body: bytes, now: datetime) -> list[Reading]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Refused("CSV must be UTF-8") from None
    table = csv.DictReader(io.StringIO(text, newline=""))
    header = tuple(table.fieldnames or ())
    if not set(CSV_COLUMNS) - {"depth_m"} <= set(header) or not set(header) <= set(CSV_COLUMNS):
        raise Refused(f"the CSV header is {','.join(CSV_COLUMNS)} (depth_m optional)")
    rows = []
    for line, row in enumerate(table, start=2):
        if None in row:
            raise Refused(f"line {line}: more cells than columns")
        result = row["result"]
        try:
            number = Decimal(result) if result else None
        except InvalidOperation:
            raise Refused(f"line {line}: result is not a number") from None
        depth = row.get("depth_m") or None
        try:
            depth_m = Decimal(depth) if depth else None
        except InvalidOperation:
            raise Refused(f"line {line}: depth_m is not a number") from None
        rows.append(
            (
                f"line {line}",
                row["observed_property"],
                row["unit"],
                row["phenomenon_time"],
                number,
                depth_m,
            )
        )
    return _readings(rows, now)


def _no_constant(name: str) -> Any:
    raise Refused(f"{name} is not a reading")


def _readings(rows: list[tuple], now: datetime) -> list[Reading]:
    if not rows:
        raise Refused("no readings")
    if len(rows) > MAX_READINGS:
        raise Refused(f"at most {MAX_READINGS} readings per push")
    readings, seen = [], set()
    for where, name, symbol, time, result, depth in rows:
        reading = _reading(where, name, symbol, time, result, depth, now)
        key = (
            reading.value.variable,
            reading.depth_m,
            reading.value.period_start,
            reading.value.period_end,
        )
        if key in seen:
            raise Refused(f"{where}: the same reading is given twice")
        seen.add(key)
        readings.append(reading)
    return readings


def _reading(where, name, symbol, time, result, depth, now: datetime) -> Reading:
    variable = VARIABLES.get(name) if isinstance(name, str) else None
    if variable is None:
        raise Refused(f"{where}: {name!r} is not a variable FarmCore stores")
    conversion = UNITS[variable.name].get(symbol) if isinstance(symbol, str) else None
    if conversion is None:
        known = ", ".join(UNITS[variable.name])
        raise Refused(f"{where}: unit {symbol!r} is not one of {known} for {variable.name}")
    if not isinstance(result, Decimal) or not result.is_finite():
        raise Refused(f"{where}: result is not a finite number")
    factor, offset = conversion
    value = result * factor + offset
    low, high = RANGES[variable.name]
    if (low is not None and value < low) or (high is not None and value > high):
        raise Refused(f"{where}: {result} {symbol} is outside what {variable.name} can be")
    start, end = _period(where, time)
    if variable.cell_method == "time: point":
        if start != end:
            raise Refused(f"{where}: {variable.name} is read at an instant, not over a period")
    elif variable.cell_method == "time: sum":
        if end - start != HOUR:
            raise Refused(f"{where}: {variable.name} is a total over exactly one hour")
    elif not timedelta(0) < end - start <= HOUR:
        raise Refused(f"{where}: {variable.name} is over a period of at most one hour")
    if end != end.replace(minute=0, second=0, microsecond=0):
        raise Refused(f"{where}: the reading must end on the hour (hourly readings)")
    if end > now + FUTURE_SKEW:
        raise Refused(f"{where}: the reading is in the future")
    if (variable.name == SOIL_MOISTURE) != (depth is not None):
        raise Refused(f"{where}: soil moisture needs depth_m, and only soil moisture has one")
    if depth is not None and (
        not isinstance(depth, Decimal) or not depth.is_finite() or not _ZERO < depth <= MAX_DEPTH_M
    ):
        raise Refused(f"{where}: depth_m is metres below the surface, up to {MAX_DEPTH_M}")
    return Reading(
        ObservationValue(variable.name, variable.unit, value, start, end, provisional=False),
        depth.normalize() if depth is not None else None,
    )


def _period(where: str, time: Any) -> tuple[datetime, datetime]:
    """An ISO 8601 instant or `start/end` interval, each with its time zone, in UTC."""
    if not isinstance(time, str):
        raise Refused(f"{where}: phenomenonTime is an ISO 8601 time")
    parts = time.split("/")
    if len(parts) > 2:
        raise Refused(f"{where}: phenomenonTime is an instant or start/end")
    try:
        times = [datetime.fromisoformat(p.strip()) for p in parts]
    except ValueError:
        raise Refused(f"{where}: phenomenonTime {time!r} is not ISO 8601") from None
    if any(t.tzinfo is None for t in times):
        raise Refused(f"{where}: phenomenonTime needs its time zone (Z or an offset)")
    start, end = (t.astimezone(UTC) for t in (times[0], times[-1]))
    if end < start:
        raise Refused(f"{where}: the period ends before it starts")
    return start, end
