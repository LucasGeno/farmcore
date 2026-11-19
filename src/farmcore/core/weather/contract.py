"""The weather provider contract.

A provider (a national service, an agricultural network, a model or an on-farm sensor) is an
object with a unique `name` and six methods:

- `discover_stations(bbox)`: the provider's stations inside a bounding box;
- `fetch_observations(station, range)`: one response's observations for a station and period;
- `fetch_forecast(point, models, run)`: one response's forecast for a point, the named models and
  a model run (None: the latest the provider serves);
- `capabilities()`: what it serves (observations, forecasts, which CF variables);
- `licence()`: the licence, attribution and whether the data may be redistributed;
- `auth_schema()`: the per-installation credentials it needs. A credential is passed to the
  provider when it is built, never put in a request it reports: the store refuses a request that
  names a secret field.

A fetch returns `Fetched`: the keyless request, the wall time the response arrived, the raw payload
(hashed into the receipt, never stored) and decimal values already in each CF variable's stored
unit (`variables.VARIABLES`). Converting units is the provider's job; the store refuses any other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Point:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Licence:
    """`identifier` is an SPDX identifier (`CC-BY-4.0`, `etalab-2.0`) or a named licence; None
    when the provider does not state one, which stores as `unknown`, never redistributable."""

    identifier: str | None
    attribution: str | None
    redistribution: bool


@dataclass(frozen=True)
class AuthField:
    name: str  # the parameter name the provider's API uses (e.g. `apikey`)
    secret: bool
    description: str = ""


@dataclass(frozen=True)
class Capabilities:
    observations: bool
    forecast: bool
    variables: frozenset[str]  # CF standard names
    countries: frozenset[str] | None = None  # ISO 3166-1 alpha-2; None: anywhere
    backfill: bool = False  # gridded history for any point (ERA5): joined as the farm's backfill


@dataclass(frozen=True)
class Station:
    provider_station_id: str
    name: str
    latitude: float
    longitude: float
    elevation_m: Decimal | None = None
    wigos_id: str | None = None


@dataclass(frozen=True)
class Request:
    """What was asked, without credentials. `endpoint_kind` says which kind of endpoint
    answered (e.g. Open-Meteo `public`, `customer` or `self-hosted`)."""

    dataset: str
    url: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    endpoint_kind: str | None = None


@dataclass(frozen=True)
class ObservationValue:
    variable: str  # CF standard name
    unit: str
    value: Decimal
    period_start: datetime
    period_end: datetime
    qc_flag: str = "unchecked"


@dataclass(frozen=True)
class ForecastValue:
    model: str
    run: datetime | None  # the model's issue time; None when the provider does not say
    valid_time: datetime
    variable: str  # CF standard name
    unit: str
    value: Decimal
    member: int = 0  # 0: the deterministic or control forecast
    period_start: datetime | None = None  # the period the value covers; None: not stated
    period_end: datetime | None = None


@dataclass(frozen=True)
class Fetched:
    request: Request
    fetched_at: datetime
    payload: bytes
    values: Sequence[ObservationValue | ForecastValue]
    library_version: str | None = None


@runtime_checkable
class WeatherProvider(Protocol):
    name: str

    def discover_stations(self, bbox: BBox) -> list[Station]: ...

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched: ...

    def fetch_forecast(
        self, point: Point, models: Sequence[str], run: datetime | None
    ) -> Fetched: ...

    def capabilities(self) -> Capabilities: ...

    def licence(self) -> Licence: ...

    def auth_schema(self) -> tuple[AuthField, ...]: ...
