"""Write provider responses into the normalised weather schema.

- `record_receipt`: one `weather_receipt` per distinct response. No licence identifier stores as
  `unknown` with redistribution blocked, whatever the provider claims; so does a stated
  `unknown`, `NOASSERTION` or `none` in any case. A request naming a secret credential field,
  or a URL with user credentials, is refused. The same payload from the same provider, dataset
  and farm returns the receipt already held (the first receipt is the evidence). The provider's
  declared merge `tier` is kept in the receipt's request metadata (`request["tier"]`), so the
  merge layer reads which tier filled an hour from the evidence itself.
- `upsert_station`: one `weather_station` per provider station ID; its owner is never cleared
  implicitly. A station whose WIGOS ID another provider's row already holds is that row (one
  physical station, one row: `wigos_id` is unique), left as that provider stored it.
- `store_observations` / `store_forecast`: decimal values in their CF variable's stored unit only
  (a float, a non-finite value, an unknown variable or another unit raises `WeatherValueError`
  before anything is written). An observation already held for the same station, variable,
  period and `provisional` keeps its value and receipt. A final value is stored beside a
  provisional one (append only), with a `revision_note` naming the value it replaces. A value
  whose key another farm's receipt already holds (a shared station) raises
  `WeatherValueError`: never silently dropped.
- `held`: the observations a farm reads as known at a time: only from its receipts fetched by
  then, a provisional value hidden once a final one for the same station, variable and period
  is among them. Current reads and as-of replay are the same read.
- `receive`: a provider's `Fetched` through all of the above.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import and_, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from farmcore.core import units
from farmcore.core.access import ScopedRepository
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
    Tier,
    WeatherProvider,
)
from farmcore.core.weather.variables import VARIABLES


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
    tier: Tier | None = None,
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
            **({"tier": Tier(tier).value} if tier is not None else {}),
            # Receipt metadata retains the provider's wording without a schema migration.
            **(
                {"derived_attribution": licence.derived_attribution}
                if licence.derived_attribution
                else {}
            ),
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
    if row is None and station.wigos_id:
        shared = session.scalar(
            select(WeatherStation).where(WeatherStation.wigos_id == station.wigos_id)
        )
        if shared is not None:
            _own(shared, station, owner_farm_id)
            session.flush()
            return shared
    if row is None:
        row = WeatherStation(provider=provider, provider_station_id=station.provider_station_id)
        session.add(row)
    row.wigos_id = station.wigos_id
    row.name = station.name
    row.geom = f"SRID=4326;POINT({station.longitude} {station.latitude})"
    row.elevation_m = station.elevation_m
    _own(row, station, owner_farm_id)
    session.flush()
    return row


def _own(row: WeatherStation, station: Station, owner_farm_id: int | None) -> None:
    if owner_farm_id is not None:
        if row.owner_farm_id not in (None, owner_farm_id):
            raise WeatherValueError(f"station {station.provider_station_id} has another owner")
        row.owner_farm_id = owner_farm_id


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
    """Insert `rows`, keeping any row already held under the same unique key. One compiled
    statement, executed for all rows (batched by the driver)."""
    if rows:
        session.execute(insert(table).on_conflict_do_nothing(), rows)


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
            "provisional": v.provisional,
            "revision_note": None,
        }
        for v in values
    ]
    if rows:
        _check_held(session, receipt, station, rows)
    _insert(session, WeatherObservation, rows)


def _key(row: Any) -> tuple:
    return (row["variable"], row["period_start"], row["period_end"], row["provisional"])


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _check_held(
    session: Session, receipt: WeatherReceipt, station: WeatherStation, rows: list[dict]
) -> None:
    """Refuse a value another farm's receipt holds under the same key; note on each final value
    the provisional one of this farm it replaces."""
    held = {
        _key(r._mapping): r
        for r in session.execute(
            select(
                WeatherObservation.variable,
                WeatherObservation.period_start,
                WeatherObservation.period_end,
                WeatherObservation.provisional,
                WeatherObservation.value,
                WeatherReceipt.farm_id,
            )
            .join(WeatherReceipt, WeatherReceipt.id == WeatherObservation.receipt_id)
            .where(
                WeatherObservation.station_id == station.id,
                WeatherObservation.variable.in_({r["variable"] for r in rows}),
                WeatherObservation.period_end >= min(r["period_end"] for r in rows),
                WeatherObservation.period_end <= max(r["period_end"] for r in rows),
            )
        )
    }
    others = [r for r in rows if _key(r) in held and held[_key(r)].farm_id != receipt.farm_id]
    if others:
        raise WeatherValueError(
            f"station {station.provider_station_id}: {len(others)} value(s) already held "
            "through another farm's receipt"
        )
    for row in rows:
        if row["provisional"]:
            continue
        replaced = held.get((*_key(row)[:3], True))
        if replaced is None or replaced.farm_id != receipt.farm_id:
            continue
        old, new = _number(replaced.value), _number(row["value"])
        row["revision_note"] = (
            f"final confirms provisional {old}"
            if replaced.value == row["value"]
            else f"final replaces provisional {old} → {new}"
        )


def held(
    repo: ScopedRepository,
    *where: Any,
    as_of: datetime | None = None,
    scope: str = "stations:read",
    order_by: Any = None,
    limit: int | None = None,
) -> list[WeatherObservation]:
    """The farm's observations matching `where` as known at `as_of` (None: all it holds): from
    its receipts fetched by then, a provisional value left out where one of them holds a final
    value for the same station, variable and period."""
    known = select(WeatherReceipt.id).where(WeatherReceipt.farm_id == repo.farm_id)
    if as_of is not None:
        known = known.where(WeatherReceipt.fetched_at <= as_of)
    final = aliased(WeatherObservation)
    replaced = exists().where(
        final.station_id == WeatherObservation.station_id,
        final.variable == WeatherObservation.variable,
        final.period_start == WeatherObservation.period_start,
        final.period_end == WeatherObservation.period_end,
        final.provisional.is_(False),
        final.receipt_id.in_(known),
    )
    return repo.list(
        WeatherObservation,
        *where,
        WeatherObservation.receipt_id.in_(known),
        ~and_(WeatherObservation.provisional, replaced),
        scope=scope,
        order_by=order_by,
        limit=limit,
    )


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
        tier=provider.capabilities().tier,
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
