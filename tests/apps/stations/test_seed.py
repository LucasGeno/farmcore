"""Stations seed: one St-Émilion station on the demo farm, idempotent."""

from __future__ import annotations

from sqlalchemy import func, select

from farmcore.core import ext_ids
from farmcore.core.models.canonical import Asset, Farm, Source


def test_seed_twice_makes_one_station_on_the_demo_farm(session, seeded):
    first = seeded()
    second = seeded()

    assert first == second == {"station": 1}
    stations = list(session.scalars(select(Asset).where(Asset.kind == "station")))
    assert len(stations) == 1
    station = stations[0]
    farm = session.scalar(select(Farm))
    assert station.farm_id == farm.id
    assert station.name == "St-Émilion 33394002"
    assert ext_ids.lookup(session, "meteofrance", "33394002") is station
    xy = session.execute(
        select(func.ST_X(Asset.geom), func.ST_Y(Asset.geom)).where(Asset.id == station.id)
    ).one()
    assert tuple(xy) == (-0.188, 44.917667)
    assert station.props["alti_m"] == 38
    assert station.props["file"] == "meteofrance/demo_station_33394002_2024_2025.csv"
    source = session.scalar(select(Source).where(Source.code == "meteofrance-33394002"))
    assert source is not None and source.licence == "Licence Ouverte"


def test_reseed_keeps_connector_health(session, seeded):
    seeded()
    station = session.scalar(select(Asset).where(Asset.kind == "station"))
    station.props = {**station.props, "last_ts": "2024-04-13T04:00:00+00:00", "rows": 12}
    session.flush()

    seeded()

    assert station.props["rows"] == 12
    assert station.props["last_ts"] == "2024-04-13T04:00:00+00:00"
    assert session.scalar(select(func.count()).select_from(Asset)) == 1
