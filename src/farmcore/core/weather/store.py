"""Write provider responses into the normalised weather schema.

- `record_receipt`: one `weather_receipt` per distinct response. No licence identifier stores as
  `unknown` with redistribution blocked, whatever the provider claims; so does a stated
  `unknown`, `NOASSERTION` or `none` in any case. A request naming a secret credential field,
  or a URL with user credentials, is refused. The same payload from the same provider, dataset
  and farm returns the receipt already held (the first receipt is the evidence).
- `upsert_station`: one `weather_station` per provider station ID; its owner is never cleared
  implicitly.
- `store_observations` / `store_forecast`: decimal values in their CF variable's stored unit only
  (a float, a non-finite value, an unknown variable or another unit raises `WeatherValueError`
  before anything is written). An observation already held for the same station, variable and
  period keeps its value and receipt.
- `receive`: a provider's `Fetched` through all of the above.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from farmcore.core import units
from farmcore.core.models.weather import (
    UNKNOWN_LICENCE,
    UNKNOWN_LICENCES,
    WeatherForecast,
    WeatherObservation,
    WeatherReceipt,
    WeatherStation,
)
from farmcore.core.weather.contract import (
    Fetched,
    ForecastValue,
    Licence,
    ObservationValue,
    Station,
    WeatherProvider,
)
from farmcore.core.weather.variables import VARIABLES

_CHUNK = 5000  # rows per INSERT, well under PostgreSQL's 65535 bind parameters


class WeatherValueError(ValueError):
    """A value the normalised schema cannot hold as given."""


class CredentialInRequest(ValueError):
    """A request to be stored names a secret credential field."""


def _check_keyless(fetched: Fetched, secret_fields: Iterable[str]) -> None:
    secrets = {name.lower() for name in secret_fields}
    named = {key.lower() for key in fetched.request.params}
    if fetched.request.url:
        url = urlsplit(fetched.request.url)
        if url.username or url.password:
            raise CredentialInRequest("the request URL carries user credentials")
        named |= {key.lower() for key, _ in parse_qsl(url.query)}
    leaked = sorted(named & secrets)
    if leaked:
        raise CredentialInRequest(f"the request names credential field(s) {', '.join(leaked)}")


def record_receipt(
    session: Session,
    *,
    provider: str,
    licence: Licence,
    fetched: Fetched,
    secret_fields: Iterable[str] = (),
    farm_id: int | None = None,
) -> WeatherReceipt:
    _check_keyless(fetched, secret_fields)
    payload_hash = sha256(fetched.payload).hexdigest()
    held = session.scalar(
        select(WeatherReceipt).where(
            WeatherReceipt.provider == provider,
            WeatherReceipt.dataset == fetched.request.dataset,
            WeatherReceipt.farm_id.is_not_distinct_from(farm_id),
            WeatherReceipt.payload_hash == payload_hash,
        )
    )
    if held is not None:
        return held
    identifier = (licence.identifier or "").strip()
    if identifier.casefold() in UNKNOWN_LICENCES or not identifier:
        identifier = UNKNOWN_LICENCE
    receipt = WeatherReceipt(
        provider=provider,
        dataset=fetched.request.dataset,
        farm_id=farm_id,
        request={
            "url": fetched.request.url,
            "params": fetched.request.params,
            "endpoint_kind": fetched.request.endpoint_kind,
        },
        fetched_at=fetched.fetched_at,
        licence=identifier,
        attribution=licence.attribution,
        redistribution=licence.redistribution and identifier != UNKNOWN_LICENCE,
        payload_hash=payload_hash,
        library_version=fetched.library_version,
    )
    session.add(receipt)
    session.flush()
    return receipt


def upsert_station(
    session: Session, provider: str, station: Station, owner_farm_id: int | None = None
) -> WeatherStation:
    """Insert or refresh a station. `owner_farm_id` None leaves the current owner as it is
    (never makes an owned station public); a different owner than the one held is refused."""
    row = session.scalar(
        select(WeatherStation).where(
            WeatherStation.provider == provider,
            WeatherStation.provider_station_id == station.provider_station_id,
        )
    )
    if row is None:
        row = WeatherStation(provider=provider, provider_station_id=station.provider_station_id)
        session.add(row)
    row.wigos_id = station.wigos_id
    row.name = station.name
    row.geom = f"SRID=4326;POINT({station.longitude} {station.latitude})"
    row.elevation_m = station.elevation_m
    if owner_farm_id is not None:
        if row.owner_farm_id not in (None, owner_farm_id):
            raise WeatherValueError(f"station {station.provider_station_id} has another owner")
        row.owner_farm_id = owner_farm_id
    session.flush()
    return row


def _checked(value: ObservationValue | ForecastValue) -> dict:
    variable = VARIABLES.get(value.variable)
    if variable is None:
        raise WeatherValueError(f"{value.variable!r} is not a stored CF variable")
    if value.unit != variable.unit:
        raise WeatherValueError(f"{value.variable} is stored in {variable.unit}, not {value.unit}")
    try:
        number = units.decimal_value(value.value)
    except units.UnitError as exc:
        raise WeatherValueError(f"{value.variable}: {exc}") from exc
    return {"variable": variable.name, "unit": variable.unit, "value": number}


def _insert(session: Session, table: type, rows: list[dict]) -> None:
    """Insert `rows`, keeping any row already held under the same unique key."""
    for i in range(0, len(rows), _CHUNK):
        session.execute(insert(table).values(rows[i : i + _CHUNK]).on_conflict_do_nothing())


def store_observations(
    session: Session,
    receipt: WeatherReceipt,
    station: WeatherStation,
    values: Sequence[ObservationValue],
) -> None:
    rows = [
        {
            **_checked(v),
            "station_id": station.id,
            "receipt_id": receipt.id,
            "period_start": v.period_start,
            "period_end": v.period_end,
            "qc_flag": v.qc_flag,
        }
        for v in values
    ]
    _insert(session, WeatherObservation, rows)


def store_forecast(
    session: Session, receipt: WeatherReceipt, values: Sequence[ForecastValue]
) -> None:
    rows = [
        {
            **_checked(v),
            "receipt_id": receipt.id,
            "model": v.model,
            "run": v.run,
            "valid_time": v.valid_time,
            "member": v.member,
            "period_start": v.period_start,
            "period_end": v.period_end,
        }
        for v in values
    ]
    _insert(session, WeatherForecast, rows)


def receive(
    session: Session,
    provider: WeatherProvider,
    fetched: Fetched,
    *,
    station: Station | None = None,
    owner_farm_id: int | None = None,
    farm_id: int | None = None,
) -> WeatherReceipt:
    """Store one provider response: receipt, station and values. `owner_farm_id` names the
    farm that owns the station (an on-farm sensor); None keeps the station's current owner."""
    observations = [v for v in fetched.values if isinstance(v, ObservationValue)]
    forecasts = [v for v in fetched.values if isinstance(v, ForecastValue)]
    for value in fetched.values:
        _checked(value)  # refuse before anything is written
    if observations and station is None:
        raise WeatherValueError("observations need their station")
    receipt = record_receipt(
        session,
        provider=provider.name,
        licence=provider.licence(),
        fetched=fetched,
        secret_fields=[f.name for f in provider.auth_schema() if f.secret],
        farm_id=farm_id,
    )
    if observations:
        store_observations(
            session,
            receipt,
            upsert_station(session, provider.name, station, owner_farm_id),
            observations,
        )
    if forecasts:
        store_forecast(session, receipt, forecasts)
    return receipt
