"""Fields seed: the demo farm's parcels from the named IGN GeoJSON, idempotently."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from farmcore.apps.fields.seed import seed
from farmcore.core import ext_ids
from farmcore.core.commands.seed import run_seeds
from farmcore.core.models.canonical import ExtId, Farm, Parcel, Source
from farmcore.core.registry import discover


def _count(session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where))


def _counts(session) -> tuple[int, int, int]:
    return (
        _count(session, Farm),
        _count(session, Parcel),
        _count(session, ExtId, ExtId.source.in_(("farmcore-demo", "ign-rpg-2024"))),
    )


def test_seed_loads_parcels_with_ext_ids_props_and_geometry(session, synthetic_data_dir):
    counts = seed(session, scene=None, data_dir=synthetic_data_dir)

    assert counts == {"parcels": 2}
    parcel = ext_ids.lookup(session, "ign-rpg-2024", "900010")
    assert isinstance(parcel, Parcel)
    assert (parcel.alias, parcel.name, parcel.iacs_id) == ("P10", "Synthetic Ten", "900010")
    assert parcel.area_ha == Decimal("1.25")
    assert parcel.props == {
        "variety": "Merlot",
        "eppo_crop": "VITVI",
        "planted_year": 2001,
        "crop_label": "Grapevine (wine grapes)",
        "code_cultu": "VRC",
    }
    farm = session.scalar(select(Farm))
    assert (farm.name, parcel.farm_id) == ("Château demo", farm.id)
    srid, gtype, lon = session.execute(
        select(
            func.ST_SRID(Parcel.geom),
            func.GeometryType(Parcel.geom),
            func.ST_XMin(Parcel.geom),
        ).where(Parcel.id == parcel.id)
    ).one()
    assert (srid, gtype, round(lon, 3)) == (4326, "MULTIPOLYGON", -0.150)
    source = session.scalar(select(Source).where(Source.code == "ign-rpg-2024"))
    assert source.licence == "Licence Ouverte 2.0"


def test_seed_inserts_parcels_in_alias_number_order(session, synthetic_data_dir):
    seed(session, scene=None, data_dir=synthetic_data_dir)

    aliases = session.scalars(select(Parcel.alias).order_by(Parcel.id)).all()
    assert aliases == ["P2", "P10"]


def test_seed_twice_changes_no_counts(session, synthetic_data_dir):
    seed(session, scene=None, data_dir=synthetic_data_dir)
    first = _counts(session)
    seed(session, scene="B", data_dir=synthetic_data_dir)

    assert first == (1, 2, 3)
    assert _counts(session) == first


def test_seed_without_the_geojson_names_the_missing_file(session, tmp_path):
    with pytest.raises(FileNotFoundError, match=r"ign/demo_parcels_named\.geojson"):
        seed(session, scene=None, data_dir=tmp_path)


@pytest.mark.data
def test_farmcore_seed_twice_on_the_real_geojson(session, data_dir):
    """AC: farm 1, parcels 10, ext_id rows for the demo + IGN sources = 11, stable on re-run."""
    fields_and_core = [m for m in discover() if m.name in ("core", "fields")]
    run_seeds(session, fields_and_core, scene=None, data_dir=data_dir)
    first = _counts(session)
    run_seeds(session, fields_and_core, scene=None, data_dir=data_dir)

    assert first == (1, 10, 11)
    assert _counts(session) == first
    aliases = session.scalars(select(Parcel.alias).order_by(Parcel.id)).all()
    assert aliases == [f"P{i}" for i in range(1, 11)]
