"""The replayed Météo-France station as a normalised weather station (expand step, core_0008).

`mirror_station` copies a station asset's `observation` rows into `weather_station`,
`weather_receipt` and `weather_observation`. `observation` stays what every consumer reads.

- The station is a public network station (no owning farm) under its Météo-France number; its
  WIGOS ID is not in the file, so it stays unknown rather than guessed.
- Periods: rain is the total over the hour ending at the timestamp; wind speed is the 10-minute
  mean ending at it (Météo-France hourly data: RR1, FF); temperature and humidity are
  instantaneous.
- Data: Météo-France, Licence Ouverte / Etalab 2.0 (redistributable with attribution).
- Values become decimals through their shortest float text, so 0.1 stays 0.1. The payload is the
  observations' canonical JSON, so mirroring the same rows again returns their receipt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from farmcore.apps.stations import connector
from farmcore.core.models.canonical import Asset, ExtId, Observation
from farmcore.core.models.weather import WeatherReceipt
from farmcore.core.weather.contract import Fetched, Licence, ObservationValue, Request, Station
from farmcore.core.weather.store import record_receipt, store_observations, upsert_station
from farmcore.core.weather.variables import BY_LEGACY

PROVIDER = "meteofrance"
LICENCE = Licence("etalab-2.0", "Météo-France, Licence Ouverte / Etalab 2.0", True)
PERIODS = {"rr1_mm": timedelta(hours=1), "ff_kmh": timedelta(minutes=10)}


def mirror_station(session: Session, asset: Asset, fetched_at: datetime) -> WeatherReceipt:
    key = session.scalar(
        select(ExtId.key).where(
            ExtId.source == connector.EXT_SOURCE, ExtId.model == "asset", ExtId.res_id == asset.id
        )
    )
    latitude, longitude = session.execute(
        select(func.ST_Y(Asset.geom), func.ST_X(Asset.geom)).where(Asset.id == asset.id)
    ).one()
    altitude = (asset.props or {}).get("alti_m")
    station = Station(
        provider_station_id=key or str(asset.id),
        name=asset.name,
        latitude=latitude,
        longitude=longitude,
        elevation_m=None if altitude is None else Decimal(repr(altitude)),
    )
    rows = session.scalars(
        select(Observation)
        .where(Observation.asset_id == asset.id)
        .order_by(Observation.ts, Observation.variable)
    ).all()
    values = [
        ObservationValue(
            variable=BY_LEGACY[o.variable].name,
            unit=BY_LEGACY[o.variable].unit,
            value=Decimal(repr(o.value)),
            period_start=o.ts - PERIODS.get(o.variable, timedelta(0)),
            period_end=o.ts,
        )
        for o in rows
        if o.variable in BY_LEGACY
    ]
    fetched = Fetched(
        request=Request(
            dataset=(asset.props or {}).get("file", connector.FILE),
            params={"station": station.provider_station_id},
        ),
        fetched_at=fetched_at,
        payload=json.dumps(
            [[o.ts.isoformat(), o.variable, repr(o.value)] for o in rows], sort_keys=True
        ).encode(),
        values=values,
    )
    receipt = record_receipt(session, provider=PROVIDER, licence=LICENCE, fetched=fetched)
    store_observations(session, receipt, upsert_station(session, PROVIDER, station), values)
    return receipt
