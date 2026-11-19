"""ERA5 history for a point from the Copernicus Climate Data Store (W1 contract, backfill only).

Sources (ECMWF and Copernicus, read 2026-11-19):

- The CDS API is the ECMWF Data Stores Processing API (an OGC API - Processes service),
  https://cds.climate.copernicus.eu/api/retrieve/v1 (OpenAPI document at `/openapi.json`).
  `POST /processes/<dataset>/execution` with `{"inputs": {...}}` queues a job (201, `jobID`);
  `GET /jobs/<id>` answers `accepted` (queued), `running`, `successful`, `failed`, `rejected` or
  `dismissed`; `GET /jobs/<id>/results` names the file (`asset.value.href`). The account's API
  key goes in the `PRIVATE-TOKEN` header, to the CDS host only. Usage limits change with the
  workload and a request that would exceed them is queued, so an order can wait for a long time
  (https://confluence.ecmwf.int/display/CKB/Climate+Data+Store+%28CDS%29+documentation).
  This module speaks that API directly rather than through `cdsapi` (Apache-2.0, 0.7.7, now a
  wrapper over `ecmwf-datastores-client`, also Apache-2.0): its blocking wait cannot be capped
  by a task's deadline, which a resumable order needs.
- Dataset `reanalysis-era5-single-levels-timeseries`: hourly ERA5 for one location (the nearest
  point of a 0.25 degree grid), `variable`, `location`, `date` (`YYYY-MM-DD/YYYY-MM-DD`, whole
  days) and `data_format` `csv`. ERA5 is updated daily about 5 days behind real time (ERA5T;
  the catalogue's constraints ended 11 days behind on 2026-11-19), and ERA5T is overwritten by
  the final ERA5 about two months later.
  https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels-timeseries
- Values: `t2m` and `d2m` in K, `u10` and `v10` in m/s (instantaneous at the valid time), `tp` in
  m accumulated over the hour ending at the valid time
  (https://confluence.ecmwf.int/display/CKB/ERA5%3A+data+documentation).
  Relative humidity is derived from `t2m` and `d2m` with the Magnus formula of Alduchov and
  Eskridge (1996, J. Appl. Meteor. 35, 601-609), constants 17.625 and 243.04 degC, over water.
  The CSV column names are not published where they could be read: they are taken as the CDS
  short names above and the parser matches them case-insensitively (to verify on a real file).
- Licence CC-BY-4.0 (https://spdx.org/licenses/CC-BY-4.0, the catalogue's `license`). The notice
  below is the wording Copernicus asks for; it could not be checked against an official page
  here.

Every value is model data (a reanalysis), not an observation: it lands under its own grid-point
station and its receipt says `endpoint_kind: model-reanalysis`.
"""

from __future__ import annotations

import csv
import io
import math
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from farmcore.core.weather.contract import (
    AuthField,
    BBox,
    Capabilities,
    Fetched,
    Licence,
    NotConfigured,
    ObservationValue,
    Point,
    Request,
    Station,
    TimeRange,
)
from farmcore.core.weather.variables import BY_LEGACY
from farmcore.settings import Settings

PROVIDER = "copernicus-era5"
DATASET = "reanalysis-era5-single-levels-timeseries"
KIND = "model-reanalysis"
NOTICE = (
    "Generated using Copernicus Climate Change Service information. "
    "Contains modified Copernicus Climate Change Service information (ERA5, ECMWF); "
    "neither the European Commission nor ECMWF is responsible for any use of this information."
)
LICENCE = Licence("CC-BY-4.0", NOTICE, True)
AUTH = (AuthField("PRIVATE-TOKEN", secret=True, description="Copernicus CDS account API key"),)
VARIABLES = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "total_precipitation",
)
# Polling an order costs a request; a 429 waits its Retry-After.
POLL_SECONDS = 10.0
KELVIN = Decimal("273.15")
MS_TO_KMH = Decimal("3.6")
HOUR = timedelta(hours=1)


class PortalError(RuntimeError):
    """The CDS refused or failed a request. The message names the step and the HTTP status,
    never the key."""


class CredentialsMissing(PortalError, NotConfigured):
    """The installation has no CDS key."""


def _decimal(text: object) -> Decimal | None:
    if text is None or not str(text).strip():
        return None
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise PortalError(f"not a number: {str(text)[:20]!r}") from None
    if not value.is_finite():
        raise PortalError("not a finite number")
    return value


def _magnus(celsius: float) -> float:
    """Saturation vapour pressure over water, hPa (Alduchov and Eskridge 1996)."""
    return 6.1094 * math.exp(17.625 * celsius / (celsius + 243.04))


def relative_humidity(air: Decimal, dew: Decimal) -> Decimal:
    """Percent, from the air and dew point temperatures in K (at most 100)."""
    ratio = _magnus(float(dew - KELVIN)) / _magnus(float(air - KELVIN))
    return Decimal(round(min(ratio, 1.0) * 100, 1)).quantize(Decimal("0.1"))


def _value(name: str, value: Decimal, end: datetime, span: timedelta = timedelta(0)):
    variable = BY_LEGACY[name]
    return ObservationValue(
        variable=variable.name,
        unit=variable.unit,
        value=value,
        period_start=end - span,
        period_end=end,
        qc_flag="unchecked",
    )


def era5_values(text: str, range: TimeRange) -> list[ObservationValue]:
    """A timeseries CSV's values, converted to the stored units, for valid times in
    `(start, end]`. Temperature, humidity and wind are instantaneous (period start = end);
    precipitation covers the hour ending at the valid time."""
    values = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        try:
            end = datetime.fromisoformat(row["valid_time"]).replace(tzinfo=UTC)
        except (KeyError, ValueError):
            raise PortalError("results: no valid_time in a row") from None
        if not range.start < end <= range.end:
            continue
        air, dew = _decimal(row.get("t2m")), _decimal(row.get("d2m"))
        u, v = _decimal(row.get("u10")), _decimal(row.get("v10"))
        rain = _decimal(row.get("tp"))
        if air is not None:
            values.append(_value("t_c", air - KELVIN, end))
        if air is not None and dew is not None:
            values.append(_value("rh_pct", relative_humidity(air, dew), end))
        if u is not None and v is not None:
            speed = Decimal(str(math.hypot(float(u), float(v)))) * MS_TO_KMH
            values.append(_value("ff_kmh", speed.quantize(Decimal("0.01")), end))
        if rain is not None:
            # Assumed: ERA5's tiny negative accumulations (numerical noise) are no rain.
            values.append(_value("rr1_mm", max(rain * 1000, Decimal(0)), end, HOUR))
    return values


class Cds:
    """Authorised calls to the CDS within one deadline (`time.monotonic()` seconds). The key
    is static (no token to renew): a 401 or 403 is final."""

    def __init__(self, key: str, *, base_url: str, deadline: float) -> None:
        self._key = key
        self.base_url, self.deadline = base_url.rstrip("/"), deadline

    def remaining(self, step: str) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise PortalError(f"{step}: out of time")
        return left

    def expired(self) -> bool:
        return self.deadline - time.monotonic() <= 0

    def send(self, step: str, method: str, url: str, **kw) -> httpx.Response:
        """One call with the key when `url` is on the CDS host; a 429 waits its Retry-After
        (capped by the deadline) and asks again."""
        headers = {}
        if urlsplit(url).netloc == urlsplit(self.base_url).netloc:
            headers["PRIVATE-TOKEN"] = self._key
        while True:
            try:
                with httpx.Client(timeout=self.remaining(step)) as client:
                    response = client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:  # its text may hold the URL; the name is enough
                raise PortalError(f"{step}: {type(exc).__name__}") from None
            if response.status_code != 429:
                return response
            try:
                wait = max(0.0, float(response.headers.get("Retry-After", POLL_SECONDS)))
            except ValueError:  # an HTTP date: wait the usual poll
                wait = POLL_SECONDS
            time.sleep(min(wait, self.remaining(step)))

    def json(self, step: str, method: str, path: str, ok: int, **kw) -> dict:
        response = self.send(step, method, f"{self.base_url}/retrieve/v1/{path}", **kw)
        if response.status_code != ok:
            raise PortalError(f"{step}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise PortalError(f"{step}: unexpected response") from None
        if not isinstance(body, dict):
            raise PortalError(f"{step}: unexpected response")
        return body


def _days(range: TimeRange) -> str:
    """The whole UTC days holding the valid times in `(start, end]`."""
    first: date = (range.start + HOUR).astimezone(UTC).date()
    return f"{first:%Y-%m-%d}/{range.end.astimezone(UTC):%Y-%m-%d}"


class Era5:
    name = PROVIDER

    def __init__(self, *, key: str | None, base_url: str, timeout: float = 60.0) -> None:
        self._key = key
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self._cds: Cds | None = None

    def cds(self) -> Cds:
        if not self._key:
            raise CredentialsMissing("Copernicus CDS key is not configured")
        if self._cds is None:
            self._cds = Cds(
                self._key, base_url=self.base_url, deadline=time.monotonic() + self.timeout
            )
        return self._cds

    def _request(self, station: Station, range: TimeRange) -> Request:
        return Request(
            dataset=DATASET,
            url=f"{self.base_url}/retrieve/v1/processes/{DATASET}/execution",
            params={
                "variable": list(VARIABLES),
                "location": {"latitude": station.latitude, "longitude": station.longitude},
                "date": [_days(range)],
                "data_format": "csv",
            },
            endpoint_kind=KIND,
        )

    def submit(self, station: Station, range: TimeRange) -> str:
        """Queue the order for `range` at the station's grid point; its job id."""
        request = self._request(station, range)
        body = self.cds().json(
            "order", "POST", f"processes/{DATASET}/execution", 201, json={"inputs": request.params}
        )
        job = body.get("jobID")
        if not isinstance(job, str) or not job:
            raise PortalError("order: no job id in the response")
        return job

    def collect(self, job: str, station: Station, range: TimeRange) -> Fetched | None:
        """The order's values once it is done; None if it is still queued or running when the
        deadline comes (collect it later with the same job id). A failed order raises."""
        cds = self.cds()
        while True:
            try:
                state = cds.json("job", "GET", f"jobs/{job}", 200).get("status")
            except PortalError:
                if cds.expired():
                    return None
                raise
            if state == "successful":
                break
            if state not in ("accepted", "running"):
                raise PortalError(f"job: {state}")
            if cds.deadline - time.monotonic() <= POLL_SECONDS:
                return None
            time.sleep(POLL_SECONDS)
        href = cds.json("results", "GET", f"jobs/{job}/results", 200)
        try:
            url = href["asset"]["value"]["href"]
        except (KeyError, TypeError):
            raise PortalError("results: no file in the response") from None
        if not isinstance(url, str) or urlsplit(url).scheme != "https":
            raise PortalError("results: not an https file")
        response = cds.send("results", "GET", url)
        if response.status_code != 200:
            raise PortalError(f"results: HTTP {response.status_code}")
        return Fetched(
            request=self._request(station, range),
            fetched_at=datetime.now(UTC),
            payload=response.content,
            values=era5_values(response.text, range),
        )

    def discover_stations(self, bbox: BBox) -> list[Station]:
        return []  # a gridded model has no stations: it serves any point

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        """Order, wait for the order within this provider's timeout, return its values."""
        job = self.submit(station, range)
        fetched = self.collect(job, station, range)
        if fetched is None:
            raise PortalError("job: out of time")
        return fetched

    def fetch_forecast(self, point: Point, models: Sequence[str], run: datetime | None) -> Fetched:
        raise NotImplementedError(f"{PROVIDER} serves history only")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            observations=False,
            forecast=False,
            variables=frozenset(BY_LEGACY[n].name for n in ("rr1_mm", "t_c", "rh_pct", "ff_kmh")),
            backfill=True,
            live_only=True,
        )

    def licence(self) -> Licence:
        return LICENCE

    def auth_schema(self) -> tuple[AuthField, ...]:
        return AUTH


def provider(settings: Settings, timeout: float = 60.0) -> Era5:
    """The manifest's provider factory (`Manifest.weather_providers`), built with the
    installation's key; `timeout` bounds every call of one run."""
    key = settings.cdsapi_key
    return Era5(
        key=key.get_secret_value() if key else None, base_url=settings.cdsapi_url, timeout=timeout
    )
