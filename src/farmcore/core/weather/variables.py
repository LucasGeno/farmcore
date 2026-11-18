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


VARIABLES: dict[str, Variable] = {
    v.name: v
    for v in (
        Variable("lwe_thickness_of_precipitation_amount", "mm", "rr1_mm"),
        Variable("air_temperature", "degC", "t_c"),
        Variable("relative_humidity", "%", "rh_pct"),
        Variable("wind_speed", "km h-1", "ff_kmh"),
        Variable("wind_speed_of_gust", "km h-1", "fx_kmh"),
    )
}
BY_LEGACY: dict[str, Variable] = {v.legacy_name: v for v in VARIABLES.values() if v.legacy_name}
