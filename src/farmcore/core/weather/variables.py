"""The weather variables FarmCore stores: a CF standard name, the unit its values are stored in,
and the legacy station/forecast variable it maps.

Names are from the CF Standard Name Table version 95 (2026-09-16,
https://cfconventions.org/Data/cf-standard-names/95/build/cf-standard-name-table.html); each
stored unit is a UDUNITS string that converts to the name's canonical unit (`mm` -> `m`,
`degC` -> `K`, `%` -> `1`, `km h-1` -> `m s-1`). Rain is the liquid-water-equivalent depth, so
millimetres stay millimetres. The database's frozen `weather_variable` table (core_0008) holds
exactly these rows; a parity test keeps them equal.
"""

from __future__ import annotations

from dataclasses import dataclass

CF_TABLE_VERSION = 95


@dataclass(frozen=True)
class Variable:
    name: str  # CF standard name
    unit: str  # the unit stored values are in (UDUNITS)
    legacy_name: str | None  # the station/forecast variable it replaces
    # How a value relates to its period (CF cell_methods): `time: point` is the value at
    # period_end (start = end), `time: sum` the total, `time: mean` the average and
    # `time: maximum` the highest value over [period_start, period_end].
    cell_method: str


VARIABLES: dict[str, Variable] = {
    v.name: v
    for v in (
        Variable("lwe_thickness_of_precipitation_amount", "mm", "rr1_mm", "time: sum"),
        Variable("air_temperature", "degC", "t_c", "time: point"),
        Variable("relative_humidity", "%", "rh_pct", "time: point"),
        # A station's mean over its averaging period (Météo-France FF: 10 min); a model's
        # instantaneous value is a mean over an empty period.
        Variable("wind_speed", "km h-1", "ff_kmh", "time: mean"),
        Variable("wind_speed_of_gust", "km h-1", "fx_kmh", "time: maximum"),
    )
}
BY_LEGACY: dict[str, Variable] = {v.legacy_name: v for v in VARIABLES.values() if v.legacy_name}
