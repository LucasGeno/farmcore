"""Open-Meteo as a weather provider (W1 contract).

`OpenMeteo.fetch_forecast(point, models, run)`:

- `run` None: the live forecast (`/v1/forecast`, the endpoint's default model, six days, UTC,
  km/h, °C, mm). The request runs within `timeout` seconds on the forecast DNS loop, and the
  response is validated (UTC, bounded consecutive hours ending after the fetch and within seven
  days, stated units, finite values inside their unit's bounds) before any value is returned.
  The model's issue time is not exposed: each value's `run` is None.
- `run` given: that Single Runs API run of the first named model (default `ecmwf_ifs`), as the
  `farmcore forecast fetch` CLI saves it.

The customer key is sent only to a customer endpoint and never reported: the `Request` carries
the keyless URL and the endpoint kind. Values are decimals through their shortest float text, in
each CF variable's stored unit; rain is the preceding hour's total and a gust its maximum
(https://open-meteo.com/en/docs), the other variables are instantaneous. Data: CC BY 4.0.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isfinite

import httpx

from farmcore.apps.forecast import sources
from farmcore.apps.forecast._fetch_loop import FetchLoop
from farmcore.core.weather.contract import (
    AuthField,
    BBox,
    Capabilities,
    Fetched,
    ForecastValue,
    Licence,
    Point,
    Request,
    Station,
    TimeRange,
)
from farmcore.core.weather.variables import BY_LEGACY
from farmcore.settings import Settings

PROVIDER = "open-meteo"
LIVE_DATASET = "open-meteo-live-receipt"
LIVE_MODEL = "best_match"  # Open-Meteo's default: the best models for the location
AUTH = (AuthField("apikey", secret=True, description="Open-Meteo customer API key"),)
LICENCE = Licence("CC-BY-4.0", "Weather data by Open-Meteo.com (CC BY 4.0)", True)
PERIODS = {"rr1_mm": timedelta(hours=1), "fx_kmh": timedelta(hours=1)}
MAX_BYTES = 256_000


def _finite(value) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def validated_values(doc, now: datetime) -> list[tuple[datetime, str, float]]:
    """The live response's values as (ts, station variable, value); raises on anything else."""
    if not isinstance(doc, dict) or doc.get("utc_offset_seconds") != 0:
        raise ValueError("UTC response required")
    hourly, units = doc["hourly"], doc["hourly_units"]
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise ValueError("hourly data and units must be objects")
    for key, bound in (("latitude", 90), ("longitude", 180)):
        value = doc.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _finite(value)
            or abs(value) > bound
        ):
            raise ValueError("invalid provider location")
    if not isinstance(hourly.get("time"), list):
        raise ValueError("hourly time array required")
    if any(not isinstance(t, str) for t in hourly["time"]):
        raise ValueError("hourly timestamps must be strings")
    times = [sources._utc(t) for t in hourly["time"]]
    if not 1 <= len(times) <= 6 * 24 or any(
        b - a != timedelta(hours=1) for a, b in zip(times, times[1:], strict=False)
    ):
        raise ValueError("bounded consecutive hourly values required")
    if times[-1] <= now or times[-1] > now + timedelta(days=7):
        raise ValueError("forecast coverage outside request")
    for name in sources.VARIABLES:
        if name not in hourly:
            continue  # missing variable is retained as missing, never zero
        if (
            not isinstance(hourly[name], list)
            or units.get(name) != sources.UNITS[name]
            or len(hourly[name]) != len(times)
        ):
            raise ValueError("incompatible units or incomplete array")
        for value in hourly[name]:
            if value is not None and not _finite(value):
                raise ValueError("non-finite weather value")
            if value is not None and (
                (name in ("precipitation", "wind_speed_10m", "wind_gusts_10m") and float(value) < 0)
                or (name == "relative_humidity_2m" and not 0 <= float(value) <= 100)
            ):
                raise ValueError("weather value outside unit bounds")
    values = sources.parse_hourly(hourly)
    if not values:
        raise ValueError("empty forecast")
    return values


async def _fetch(url: str, timeout: float) -> bytes:
    # A read timeout alone permits an endless drip. This deadline cancels header
    # and body waits, then unwinds both contexts before the next farm can run.
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise ValueError("response too large")
                    body.extend(chunk)
            return bytes(body)


def forecast_values(
    values: Sequence[tuple[datetime, str, float]], model: str, run: datetime | None
) -> list[ForecastValue]:
    """(ts, station variable, value) as CF forecast values with their periods."""
    return [
        ForecastValue(
            model=model,
            run=run,
            valid_time=ts,
            variable=BY_LEGACY[variable].name,
            unit=BY_LEGACY[variable].unit,
            value=Decimal(repr(value)),
            period_start=ts - PERIODS.get(variable, timedelta(0)),
            period_end=ts,
        )
        for ts, variable, value in values
    ]


def legacy_values(fetched: Fetched) -> list[tuple[datetime, str, float]]:
    """A forecast response's values under the station variable names, as floats."""
    by_cf = {v.name: legacy for legacy, v in BY_LEGACY.items()}
    return [
        (v.valid_time, by_cf[v.variable], float(v.value))
        for v in fetched.values
        if isinstance(v, ForecastValue)
    ]


class OpenMeteo:
    name = PROVIDER

    def __init__(
        self,
        *,
        base_url: str,
        single_runs_base_url: str = sources.SINGLE_RUNS_BASE_URL,
        api_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url, self.single_runs_base_url = base_url, single_runs_base_url
        self._api_key, self.timeout = api_key, timeout

    def discover_stations(self, bbox: BBox) -> list[Station]:
        return []  # a model: no stations

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        raise NotImplementedError("Open-Meteo is used for forecasts only")

    def fetch_forecast(self, point: Point, models: Sequence[str], run: datetime | None) -> Fetched:
        if run is not None:
            return self._single_run(point, models[0] if models else sources.SINGLE_RUNS_MODEL, run)
        request_url = sources.forecast_url(
            self.base_url,
            {
                "latitude": point.latitude,
                "longitude": point.longitude,
                "hourly": ",".join(sources.VARIABLES),
                "forecast_days": 6,
                "timezone": "UTC",
                "wind_speed_unit": "kmh",
                "temperature_unit": "celsius",
                "precipitation_unit": "mm",
            },
        )
        url = sources.with_api_key(request_url, self.base_url, self._api_key)
        with asyncio.Runner(loop_factory=FetchLoop) as runner:
            payload = runner.run(_fetch(url, self.timeout))
        received = datetime.now(UTC)
        values = validated_values(json.loads(payload), received)
        return Fetched(
            request=Request(
                dataset=LIVE_DATASET,
                url=request_url,
                endpoint_kind=sources.endpoint_kind(self.base_url),
            ),
            fetched_at=received,
            payload=payload,
            values=forecast_values(values, LIVE_MODEL, None),
        )

    def _single_run(self, point: Point, model: str, run: datetime) -> Fetched:
        base_url = self.single_runs_base_url
        request_url = sources.single_run_url(run, point.latitude, point.longitude, base_url)
        doc = sources.get_json(sources.with_api_key(request_url, base_url, self._api_key))
        return Fetched(
            request=Request(
                dataset=f"{sources.SINGLE_RUNS}/{model}",
                url=request_url,
                endpoint_kind=sources.endpoint_kind(base_url),
            ),
            fetched_at=datetime.now(UTC),
            payload=json.dumps(doc, ensure_ascii=False).encode(),
            values=forecast_values(sources.parse_hourly(doc["hourly"]), model, run),
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            observations=False,
            forecast=True,
            variables=frozenset(BY_LEGACY[v].name for v in sources.VARIABLES.values()),
        )

    def licence(self) -> Licence:
        return LICENCE

    def auth_schema(self) -> tuple[AuthField, ...]:
        return AUTH


def provider(settings: Settings, timeout: float | None = None) -> OpenMeteo:
    """The manifest's provider factory (`Manifest.weather_providers`), built with the
    installation's endpoints and key; `timeout` bounds one live request (default: settings)."""
    key = settings.open_meteo_api_key
    return OpenMeteo(
        base_url=settings.open_meteo_base_url,
        single_runs_base_url=settings.open_meteo_single_runs_base_url,
        api_key=key.get_secret_value() if key is not None else None,
        timeout=settings.forecast_fetch_timeout_seconds if timeout is None else timeout,
    )
