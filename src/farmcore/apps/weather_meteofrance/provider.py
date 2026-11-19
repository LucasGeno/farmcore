"""Live Météo-France station observations from the portal APIs (W1 contract).

Sources (Météo-France's portal, read 2026-11-19):

- Token: `POST https://portail-api.meteofrance.fr/token`, `grant_type=client_credentials`, the
  application's id and secret as HTTP Basic; a bearer token valid one hour. Calls carry
  `Authorization: Bearer`. The credentials go only to the token URL, and no request this module
  reports, error it raises or line it logs carries them or the token.
- DPObs v2 (`/public/DPObs/v2`, 100 requests/min): `GET /station/horaire?id_station=<8
  digits>&date=<hour>&format=json` returns one hour, nearest the date asked, kept 24 hours.
  `validity_time` is the hour; `t` is kelvin, `ff` the 10-minute mean wind at 10 m in m/s,
  `rr1` the hour's rain in mm, `u` relative humidity in %. A row for another hour is dropped.
  `GET /liste-stations` is the station catalogue (CSV, `;`).
- DPClim v1 (`/public/DPClim/v1`, 50 requests/min): `GET /commande-station/horaire?id-station=
  &date-deb-periode=&date-fin-periode=` orders a period (202, order id); `GET /commande/fichier?
  id-cmde=` returns the CSV (201) or 204 while it is produced. Columns `POSTE;DATE(AAAAMMJJHH);
  RR1;T;U;FF;…` in mm, °C, %, m/s, each with its quality code `Q<name>`: 0 protected and 1
  validated (`passed`), 2 doubtful (`suspect`), 9 filtered only (`unchecked`).
- https://portail-api.meteofrance.fr (API pages and their OpenAPI documents);
  https://confluence-meteofrance.atlassian.net/wiki/spaces/OpenDataMeteoFrance/pages/853639294
  (DPObs guide), .../pages/854261785 (DPClim guide); field lists:
  https://www.data.gouv.fr/api/1/datasets/r/aaae09a4-4d5e-4191-a774-0a74d5214322 (DPObs),
  https://www.data.gouv.fr/api/1/datasets/r/c7da02fc-bc66-412b-a2fb-0b9b7920e794 (DPClim hourly).

`fetch_observations(station, range)` asks DPClim when the whole `(start, end]` is older than the
24 hours DPObs keeps, DPObs otherwise; one call is one receipt. Station numbers are the replay's
(`stations.connector.EXT_SOURCE` keys), so both write the same normalised station. Data:
Licence Ouverte / Etalab 2.0.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from farmcore.core.weather.contract import (
    AuthField,
    BBox,
    Capabilities,
    Fetched,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    TimeRange,
)
from farmcore.core.weather.variables import BY_LEGACY
from farmcore.settings import Settings

PROVIDER = "meteofrance-portal"
LICENCE = Licence("etalab-2.0", "Météo-France, Licence Ouverte / Etalab 2.0", True)
AUTH = (
    AuthField("client_id", secret=True, description="Météo-France portal application id"),
    AuthField("client_secret", secret=True, description="Météo-France portal application secret"),
)
OBS = "DPObs/v2"
CLIM = "DPClim/v1"
RECENT = timedelta(hours=24)  # what DPObs keeps
# The portal's guide asks to wait a few seconds before fetching an order, then poll; 5 s keeps
# one farm's polls at 12 of DPClim's 50 requests a minute. A 429 waits its Retry-After.
FIRST_POLL_SECONDS = 3.0
POLL_SECONDS = 5.0
KELVIN = Decimal("273.15")
MS_TO_KMH = Decimal("3.6")
QC = {"0": "passed", "1": "passed", "2": "suspect"}
PERIODS = {"rr1_mm": timedelta(hours=1), "ff_kmh": timedelta(minutes=10)}


class PortalError(RuntimeError):
    """The portal refused or failed a request. The message names the step and the HTTP status,
    never a credential or the token."""


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_end(hour: datetime) -> str:
    """The DPClim order's end for the last hour wanted: `hh:59:59Z`, its documented form."""
    return hour.astimezone(UTC).strftime("%Y-%m-%dT%H:59:59Z")


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", POLL_SECONDS)))
    except ValueError:  # an HTTP date: wait the usual poll
        return POLL_SECONDS


def _decimal(text: object) -> Decimal | None:
    """A number from a JSON value or a CSV cell (decimal comma or point); None when empty."""
    if text is None or (isinstance(text, str) and not text.strip()) or isinstance(text, bool):
        return None
    try:
        value = Decimal(
            str(text).strip().replace(",", ".") if isinstance(text, str) else repr(text)
        )
    except InvalidOperation:
        raise PortalError(f"not a number: {str(text)[:20]!r}") from None
    if not value.is_finite():
        raise PortalError("not a finite number")
    return value


def _value(name: str, value: Decimal, end: datetime, qc: str = "unchecked") -> ObservationValue:
    variable = BY_LEGACY[name]
    return ObservationValue(
        variable=variable.name,
        unit=variable.unit,
        value=value,
        period_start=end - PERIODS.get(name, timedelta(0)),
        period_end=end,
        qc_flag=qc,
    )


def recent_values(rows: Sequence[dict], hour: datetime) -> list[ObservationValue]:
    """One DPObs hour's values (`hour`'s row only), converted to the stored units."""
    values = []
    for row in rows:
        if row.get("validity_time") != _iso(hour):
            continue
        for name, key, convert in (
            ("t_c", "t", lambda v: v - KELVIN),
            ("rh_pct", "u", lambda v: v),
            ("ff_kmh", "ff", lambda v: v * MS_TO_KMH),
            ("rr1_mm", "rr1", lambda v: v),
        ):
            number = _decimal(row.get(key))
            if number is not None:
                values.append(_value(name, convert(number), hour))
    return values


def archive_values(text: str, range: TimeRange) -> list[ObservationValue]:
    """A DPClim hourly file's values with their quality, for hours in `(start, end]`."""
    values = []
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        row = {k.strip().upper(): (v or "").strip() for k, v in row.items() if k}
        end = datetime.strptime(row["DATE"], "%Y%m%d%H").replace(tzinfo=UTC)
        if not range.start < end <= range.end:
            continue
        for name, key, convert in (
            ("t_c", "T", lambda v: v),
            ("rh_pct", "U", lambda v: v),
            ("ff_kmh", "FF", lambda v: v * MS_TO_KMH),
            ("rr1_mm", "RR1", lambda v: v),
        ):
            number = _decimal(row.get(key))
            if number is not None:
                qc = QC.get(row.get(f"Q{key}", ""), "unchecked")
                values.append(_value(name, convert(number), end, qc))
    return values


class Portal:
    """Authorised calls to the portal within one deadline (`time.monotonic()` seconds).

    The token is minted once per Portal and never refreshed on a clock: a Portal lives for one
    run (`ingest.DEADLINE`, minutes), far inside the token's one hour. A 401 renews it once."""

    def __init__(
        self, client_id: str, client_secret: str, *, token_url: str, deadline: float
    ) -> None:
        self._auth = (client_id, client_secret)
        self._token: str | None = None
        self.token_url, self.deadline = token_url, deadline

    def remaining(self, step: str) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise PortalError(f"{step}: out of time")
        return left

    def _send(self, step: str, method: str, url: str, **kw) -> httpx.Response:
        try:
            with httpx.Client(timeout=self.remaining(step)) as client:
                return client.request(method, url, **kw)
        except httpx.HTTPError as exc:  # its text may hold the URL; the name is enough
            raise PortalError(f"{step}: {type(exc).__name__}") from None

    def _bearer(self) -> str:
        if self._token is None:
            response = self._send(
                "token",
                "POST",
                self.token_url,
                auth=self._auth,
                data={"grant_type": "client_credentials"},
            )
            if response.status_code != 200:
                raise PortalError(f"token: HTTP {response.status_code}")
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise PortalError("token: no access token in the response")
            self._token = token
        return self._token

    def get(self, step: str, url: str, params: dict[str, str]) -> httpx.Response:
        """GET with the bearer token; a token refused once (expired) is renewed once."""
        for renew in (False, True):
            if renew:
                self._token = None
            response = self._send(
                step,
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._bearer()}"},
            )
            if response.status_code != 401:
                return response
        raise PortalError(f"{step}: HTTP 401")


class MeteoFranceLive:
    name = PROVIDER

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        base_url: str,
        token_url: str,
        timeout: float = 60.0,
    ) -> None:
        self._credentials = (client_id, client_secret)
        self.base_url, self.token_url, self.timeout = base_url.rstrip("/"), token_url, timeout
        self._portal: Portal | None = None

    def portal(self) -> Portal:
        client_id, client_secret = self._credentials
        if not client_id or not client_secret:
            raise PortalError("Météo-France portal credentials are not configured")
        if self._portal is None:
            self._portal = Portal(
                client_id,
                client_secret,
                token_url=self.token_url,
                deadline=time.monotonic() + self.timeout,
            )
        return self._portal

    def discover_stations(self, bbox: BBox) -> list[Station]:
        """DPObs stations inside `bbox`, from its catalogue."""
        response = self.portal().get("stations", f"{self.base_url}/{OBS}/liste-stations", {})
        if response.status_code != 200:
            raise PortalError(f"stations: HTTP {response.status_code}")
        stations = []
        for row in csv.DictReader(io.StringIO(response.text), delimiter=";"):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            latitude, longitude = _decimal(row.get("latitude")), _decimal(row.get("longitude"))
            if latitude is None or longitude is None:
                continue
            if bbox.west <= longitude <= bbox.east and bbox.south <= latitude <= bbox.north:
                stations.append(
                    Station(
                        provider_station_id=row["id_station"].zfill(8),
                        name=row.get("nom_usuel") or row["id_station"],
                        latitude=float(latitude),
                        longitude=float(longitude),
                        elevation_m=_decimal(row.get("altitude")),
                    )
                )
        return stations

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        if range.end <= now - RECENT:
            return self._archive(station, range)
        return self._recent(station, range)

    def _recent(self, station: Station, range: TimeRange) -> Fetched:
        url = f"{self.base_url}/{OBS}/station/horaire"
        bodies, values = [], []
        hour = range.start + timedelta(hours=1)
        while hour <= range.end:
            params = {
                "id_station": station.provider_station_id,
                "date": _iso(hour),
                "format": "json",
            }
            response = self.portal().get("observations", url, params)
            if response.status_code != 200:
                raise PortalError(f"observations: HTTP {response.status_code}")
            rows = response.json()
            if not isinstance(rows, list):
                raise PortalError("observations: unexpected response")
            bodies.append(rows)
            values += recent_values(rows, hour)
            hour += timedelta(hours=1)
        return Fetched(
            request=Request(
                dataset=f"{OBS}/station/horaire",
                url=url,
                params={
                    "id_station": station.provider_station_id,
                    "from": _iso(range.start),
                    "to": _iso(range.end),
                },
            ),
            fetched_at=datetime.now(UTC),
            payload=json.dumps(bodies, sort_keys=True).encode(),
            values=values,
        )

    def _archive(self, station: Station, range: TimeRange) -> Fetched:
        url = f"{self.base_url}/{CLIM}/commande-station/horaire"
        params = {
            "id-station": station.provider_station_id,
            "date-deb-periode": _iso(range.start + timedelta(hours=1)),
            "date-fin-periode": _iso_end(range.end),
        }
        portal = self.portal()
        response = portal.get("archive order", url, params)
        if response.status_code != 202:
            raise PortalError(f"archive order: HTTP {response.status_code}")
        try:
            order = str(response.json()["elaboreProduitAvecDemandeResponse"]["return"])
        except (ValueError, KeyError, TypeError):
            raise PortalError("archive order: no order id in the response") from None
        time.sleep(min(FIRST_POLL_SECONDS, portal.remaining("archive file")))
        while True:
            file = portal.get(
                "archive file", f"{self.base_url}/{CLIM}/commande/fichier", {"id-cmde": order}
            )
            if file.status_code == 201:
                break
            if file.status_code not in (204, 429):  # 204: still produced; 429: over quota
                raise PortalError(f"archive file: HTTP {file.status_code}")
            time.sleep(min(_retry_after(file), portal.remaining("archive file")))
        return Fetched(
            request=Request(dataset=f"{CLIM}/commande-station/horaire", url=url, params=params),
            fetched_at=datetime.now(UTC),
            payload=file.content,
            values=archive_values(file.text, range),
        )

    def fetch_forecast(self, point: Point, models: Sequence[str], run: datetime | None) -> Fetched:
        raise NotImplementedError(f"{PROVIDER} serves observations only")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            observations=True,
            forecast=False,
            variables=frozenset(
                BY_LEGACY[name].name for name in ("rr1_mm", "t_c", "rh_pct", "ff_kmh")
            ),
            countries=frozenset({"FR"}),
        )

    def licence(self) -> Licence:
        return LICENCE

    def auth_schema(self) -> tuple[AuthField, ...]:
        return AUTH


def provider(settings: Settings, timeout: float = 60.0) -> MeteoFranceLive:
    """The manifest's provider factory (`Manifest.weather_providers`), built with the
    installation's credentials; `timeout` bounds every call of one run."""
    client_id, client_secret = settings.meteofrance_client_id, settings.meteofrance_client_secret
    return MeteoFranceLive(
        client_id=client_id.get_secret_value() if client_id else None,
        client_secret=client_secret.get_secret_value() if client_secret else None,
        base_url=settings.meteofrance_api_base_url,
        token_url=settings.meteofrance_token_url,
        timeout=timeout,
    )
