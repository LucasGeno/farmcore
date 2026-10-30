"""Operator provisioning through the CLI; owners consume the result over HTTP."""

import json

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from farmcore.cli import cli
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.settings import get_settings


def feature(alias="V1"):
    return {
        "type": "Feature",
        "properties": {
            "alias": alias,
            "name": "Synthetic vines",
            "area_ha": 1.2,
            "eppo_crop": "VITVI",
            "variety": "Merlot",
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[2, 44], [2.001, 44], [2.001, 44.001], [2, 44.001], [2, 44]]],
        },
    }


@pytest.fixture
def provision(committed_session_factory, settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    with committed_session_factory() as session:
        farms = [Farm(name="Synthetic A"), Farm(name="Synthetic B")]
        session.add_all(farms)
        session.commit()
        ids = [f.id for f in farms]

    def invoke(features, farm_id=ids[0]):
        path = tmp_path / "parcels.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        return CliRunner().invoke(cli, ["parcels", "import", str(path), "--farm-id", str(farm_id)])

    yield invoke, ids
    get_settings.cache_clear()


def test_import_is_farm_explicit_and_repeatable(provision, committed_session_factory):
    invoke, ids = provision
    for farm_id in ids:
        result = invoke([feature()], farm_id)
        assert result.exit_code == 0, result.output
        assert invoke([feature()], farm_id).exit_code == 0
    with committed_session_factory() as session:
        rows = session.execute(
            select(Parcel.farm_id, Parcel.alias, Parcel.props, func.ST_GeometryType(Parcel.geom))
        ).all()
        assert sorted((r.farm_id, r.alias) for r in rows) == [(ids[0], "V1"), (ids[1], "V1")]
        assert all(r.props == {"eppo_crop": "VITVI", "variety": "Merlot"} for r in rows)
        assert all(r[3] == "ST_MultiPolygon" for r in rows)


@pytest.mark.parametrize("bad", ["conflict", "geometry", "range", "area", "authority", "duplicate"])
def test_rejected_batch_preserves_existing_parcels(provision, committed_session_factory, bad):
    invoke, ids = provision
    assert invoke([feature()]).exit_code == 0
    invalid = feature("V3")
    if bad == "conflict":
        invalid = feature()
        invalid["properties"]["name"] = "Changed name"
    elif bad == "geometry":
        invalid["geometry"]["coordinates"] = [
            [[2, 44], [2.001, 44.001], [2, 44.001], [2.001, 44], [2, 44]]
        ]
    elif bad == "range":
        invalid["geometry"]["coordinates"][0][0] = [200, 44]
    elif bad == "area":
        invalid["properties"]["area_ha"] = -1
    elif bad == "authority":
        invalid["properties"]["farm_id"] = ids[1]
    else:
        invalid = feature("V2")
    result = invoke([feature("V2"), invalid])
    assert result.exit_code != 0, result.output
    with committed_session_factory() as session:
        assert session.execute(select(Parcel.alias, Parcel.name)).all() == [
            ("V1", "Synthetic vines")
        ]
