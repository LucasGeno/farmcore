"""The Météo-France replay station as a weather provider (W1 contract).

`MeteoFranceReplay` reads the demo station CSV (`connector`) under `data_dir`: it discovers the
one station it holds and answers `fetch_observations` with that station's non-null cells in
`(range.start, range.end]`, in each CF variable's stored unit. No network, no credentials.

- Periods: rain is the total over the hour ending at the timestamp; wind speed is the 10-minute
  mean ending at it (Météo-France hourly RR1, FF); temperature and humidity are instantaneous.
- Values become decimals through their shortest float text, so 0.1 stays 0.1.
- Data: Météo-France, Licence Ouverte / Etalab 2.0 (redistributable with attribution). The
  station's WIGOS ID is not in the file, so it stays unknown rather than guessed.

`store_hours` is the ingest's write path: the legacy `observation` rows every older reader still
reads, and the same cells through the contract into the normalised schema, under the farm that
fetched them (`receive`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from farmcore.apps.stations import connector
from farmcore.core.models.canonical import Asset, ExtId
from farmcore.core.weather.contract import (
    BBox,
    Capabilities,
    Fetched,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    Tier,
    TimeRange,
)
from farmcore.core.weather.store import receive
from farmcore.core.weather.variables import BY_LEGACY
from farmcore.settings import Settings

PROVIDER = "meteofrance"
LICENCE = Licence("etalab-2.0", "Météo-France, Licence Ouverte / Etalab 2.0", True)
PERIODS = {"rr1_mm": timedelta(hours=1), "ff_kmh": timedelta(minutes=10)}
# The demo station the CSV holds (stations seed: St-Émilion, 38 m).
STATION = Station(
    provider_station_id=connector.STATION_KEY,
    name="St-Émilion 33394002",
    latitude=44.917667,
    longitude=-0.188,
    elevation_m=Decimal(38),
)


def values(hours: Sequence[connector.Hour]) -> list[ObservationValue]:
    """The non-null cells of `hours` as CF observation values (a null cell gives none)."""
    return [
        ObservationValue(
            variable=BY_LEGACY[name].name,
            unit=BY_LEGACY[name].unit,
            value=Decimal(repr(x)),
            period_start=h.ts - PERIODS.get(name, timedelta(0)),
            period_end=h.ts,
        )
        for h in hours
        for name, x in h.values.items()
        if x is not None
    ]


def fetched(hours: Sequence[connector.Hour], file: str, station_id: str, at: datetime) -> Fetched:
    """One response: `hours` of `file`, read at `at`. The payload is the cells' canonical JSON,
    so the same hours read again are the same receipt."""
    return Fetched(
        request=Request(dataset=file, params={"station": station_id}),
        fetched_at=at,
        payload=json.dumps(
            [[h.ts.isoformat(), name, repr(x)] for h in hours for name, x in h.values.items()],
            sort_keys=True,
        ).encode(),
        values=values(hours),
    )


class MeteoFranceReplay:
    name = PROVIDER

    def __init__(self, data_dir: Path, file: str = connector.FILE) -> None:
        self.data_dir, self.file = data_dir, file

    def discover_stations(self, bbox: BBox) -> list[Station]:
        inside = (
            bbox.west <= STATION.longitude <= bbox.east
            and bbox.south <= STATION.latitude <= bbox.north
        )
        return [STATION] if inside else []

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        if station.provider_station_id != STATION.provider_station_id:
            raise ValueError(f"{PROVIDER} replay has no station {station.provider_station_id}")
        hours = [
            h
            for h in connector.read_hours(self.data_dir / self.file)
            if range.start < h.ts <= range.end
        ]
        return fetched(hours, self.file, station.provider_station_id, range.end)

    def fetch_forecast(self, point: Point, models: Sequence[str], run: datetime | None) -> Fetched:
        raise NotImplementedError(f"{PROVIDER} replay serves observations only")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            observations=True,
            forecast=False,
            variables=frozenset(BY_LEGACY[name].name for name in connector.VARIABLES),
            countries=frozenset({"FR"}),
            tier=Tier.NATIONAL,
        )

    def licence(self) -> Licence:
        return LICENCE

    def auth_schema(self) -> tuple:
        return ()


def provider(settings: Settings) -> MeteoFranceReplay:
    """The manifest's provider factory (`Manifest.weather_providers`)."""
    return MeteoFranceReplay(settings.data_dir)


def station_of(session: Session, asset: Asset) -> Station:
    """`asset` as its provider station: its Météo-France number (the `meteofrance` external
    key), name, location and altitude. A farm's own sensor is never published as a Météo-France
    station: an asset without the key raises."""
    key = session.scalar(
        select(ExtId.key).where(
            ExtId.source == connector.EXT_SOURCE, ExtId.model == "asset", ExtId.res_id == asset.id
        )
    )
    if key is None:
        raise ValueError(f"asset {asset.id} is not a Météo-France station")
    latitude, longitude = session.execute(
        select(func.ST_Y(Asset.geom), func.ST_X(Asset.geom)).where(Asset.id == asset.id)
    ).one()
    altitude = (asset.props or {}).get("alti_m")
    return Station(
        provider_station_id=key,
        name=asset.name,
        latitude=latitude,
        longitude=longitude,
        elevation_m=None if altitude is None else Decimal(repr(altitude)),
    )


def store_hours(
    session: Session, asset: Asset, hours: Sequence[connector.Hour], at: datetime
) -> None:
    """Store `hours` read at clock time `at` for the station `asset`: legacy rows, then the
    normalised receipt and observations, owned by the asset's farm."""
    if not hours:
        return
    station = station_of(session, asset)  # refuses before anything is written
    connector.upsert(session, asset.id, connector.observations(hours))
    file = (asset.props or {}).get("file", connector.FILE)
    receive(
        session,
        MeteoFranceReplay(Path(), file),
        fetched(hours, file, station.provider_station_id, at),
        station=station,
        farm_id=asset.farm_id,
    )
