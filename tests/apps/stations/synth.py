"""Synthetic Météo-France station CSVs (made-up values, the real file's columns)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

HEADER = "num_poste,nom_usuel,lat,lon,alti_m,timestamp_utc,rr1_mm,t_c,rh_pct,ff_ms,ff_kmh,fxi_ms"
FILE = "meteofrance/demo_station_33394002_2024_2025.csv"

Row = tuple[datetime, str, str, str, str]


def write_csv(data_dir: Path, rows: list[Row]) -> Path:
    """rows: (ts UTC, rr1_mm, t_c, rh_pct, ff_kmh) as CSV text; '' is a null cell."""
    path = data_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [HEADER]
    for ts, rr1, t, rh, ff in rows:
        stamp = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"33394002,ST EMILION,44.917667,-0.188,38,{stamp},{rr1},{t},{rh},,{ff},")
    path.write_text("\n".join(lines) + "\n")
    return path


def hourly(start: datetime, hours: int) -> list[Row]:
    """`hours` complete synthetic rows from `start`."""
    return [
        (start + timedelta(hours=i), "0.2", f"{10 + i % 5}.0", "80", "7.0") for i in range(hours)
    ]


class _Synthetic:
    """A test-only provider for synthetic station assets (no Météo-France number)."""

    name = "synthetic"

    def licence(self):
        from farmcore.core.weather.contract import Licence

        return Licence(None, None, False)

    def auth_schema(self):
        return ()


def observe(session, station, rows) -> None:
    """Store `rows` ((ts, station variable, value), the variables of `connector.VARIABLES`) for
    the station asset: legacy rows and the normalised schema, as the replay ingest does. A
    Météo-France station goes through its provider; any other asset is given a `synthetic`
    provider station (external key `synthetic`:`station-<id>`) and a location first."""
    from sqlalchemy import select

    from farmcore.apps.stations import connector, provider
    from farmcore.core.models.canonical import ExtId
    from farmcore.core.weather.contract import Station
    from farmcore.core.weather.store import receive

    session.flush()
    if station.geom is None:
        station.geom = "SRID=4326;POINT(-0.188 44.917667)"
    session.flush()
    by_ts: dict = {}
    for ts, variable, value in rows:
        by_ts.setdefault(ts, dict.fromkeys(connector.VARIABLES))[variable] = value
    hours = [connector.Hour(ts, values) for ts, values in sorted(by_ts.items())]
    sources = session.scalars(
        select(ExtId.source).where(ExtId.model == "asset", ExtId.res_id == station.id)
    ).all()
    if connector.EXT_SOURCE in sources:
        provider.store_hours(session, station, hours, max(by_ts))
        return
    key = f"station-{station.id}"
    if _Synthetic.name not in sources:
        session.add(ExtId(source=_Synthetic.name, key=key, model="asset", res_id=station.id))
    connector.upsert(session, station.id, connector.observations(hours))
    receive(
        session,
        _Synthetic(),
        provider.fetched(hours, "synthetic", key, max(by_ts)),
        station=Station(key, station.name, 44.917667, -0.188),
        farm_id=station.farm_id,
    )
    session.flush()


def correct(session, station, ts, variable, value) -> None:
    """Change one stored reading of the station (legacy and normalised), as a later correction
    of the source would."""
    from sqlalchemy import update

    from farmcore.apps.stations import service
    from farmcore.core.models.canonical import Observation
    from farmcore.core.models.weather import WeatherObservation
    from farmcore.core.weather.variables import BY_LEGACY

    session.execute(
        update(Observation)
        .where(Observation.asset_id == station.id, Observation.ts == ts)
        .where(Observation.variable == variable)
        .values(value=value)
    )
    session.execute(
        update(WeatherObservation)
        .where(
            WeatherObservation.station_id.in_(service.weather_stations_of(station.id)),
            WeatherObservation.period_end == ts,
            WeatherObservation.variable == BY_LEGACY[variable].name,
        )
        .values(value=value)
    )
    session.flush()
