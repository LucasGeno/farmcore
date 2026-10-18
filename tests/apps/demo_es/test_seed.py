"""Scene B includes a source-backed Spanish archive, never French demo treatments."""

import pytest
from sqlalchemy import func, select

from farmcore.core import registry
from farmcore.core.commands.seed import run_seeds
from farmcore.core.models.canonical import Asset, Farm, Log, Observation, Parcel, Source

pytestmark = pytest.mark.spanish_data


@pytest.mark.data
def test_scene_b_seeds_two_selectable_farms_and_ten_sigpac_vineyards(client, session, data_dir):
    manifests = registry.discover(packages=("farmcore.apps",))
    run_seeds(session, manifests, scene="B", data_dir=data_dir)
    session.commit()
    assert session.scalar(select(Source).where(Source.code == "sigpac-2024")).licence == (
        "SIGPAC 2024 · FEGA/MAPA and Junta de Castilla y León (ITACyL). "
        "Licence: CC BY 4.0 (FEGA, national) / IGCYL-NC "
        "(Junta de Castilla y León regional catalogue, non-commercial). "
        "Used for a non-commercial demo with attribution."
    )
    assert session.scalar(select(Source).where(Source.code == "inforiego")).licence == (
        "InfoRiego · ITACyL, station VA07. Reuse terms set by ITACyL; "
        "not redistributed here, shown with attribution to ITACyL."
    )
    farms = session.scalars(select(Farm).order_by(Farm.id)).all()
    assert len(farms) == 2
    french, spanish = farms
    assert spanish.props["country"] == "ES" and spanish.props["region"] == "ES418"
    plots = session.scalars(select(Parcel).where(Parcel.farm_id == spanish.id)).all()
    assert len(plots) == 10
    assert {p.props["uso_sigpac"] for p in plots} == {"VI"}
    assert {p.iacs_id for p in plots} == {
        f"47:180:0:0:9:{parcel}:{recinto}"
        for parcel, recinto in [
            (250, 1),
            (245, 1),
            (227, 1),
            (229, 1),
            (267, 1),
            (261, 1),
            (225, 1),
            (230, 4),
            (242, 1),
            (231, 1),
        ]
    }
    assert all(
        session.scalar(select(func.ST_IsValid(Parcel.geom)).where(Parcel.id == p.id)) for p in plots
    )
    assert float(sum(p.area_ha for p in plots)) == pytest.approx(14.197018, abs=0.000001)
    station = session.scalar(select(Asset).where(Asset.farm_id == spanish.id))
    assert station.geom is None
    assert station.props["archive"]["rows"] == 11385
    assert station.props["archive"]["missing_slots"] == 375
    assert station.props["archive"]["first"] == "2024-03-01 00:30"
    assert station.props["archive"]["last"] == "2024-11-01 00:00"
    assert not session.scalar(select(Observation.id).where(Observation.asset_id == station.id))
    assert not session.scalar(select(Log.id).where(Log.farm_id == spanish.id))
    assert len(session.scalars(select(Log).where(Log.farm_id == french.id)).all()) == 10
    switcher = client.get("/farms/switcher").text
    assert spanish.name in switcher and french.name in switcher
    ids = [p.id for p in plots]
    run_seeds(session, manifests, scene="B", data_dir=data_dir)
    session.commit()
    assert set(session.scalars(select(Parcel.id).where(Parcel.farm_id == spanish.id))) == set(ids)
    assert len(session.scalars(select(Farm)).all()) == 2
