"""Fields test helpers: a synthetic named-parcels GeoJSON (2 squares near Saint-Émilion,
not IGN data) written to a temporary `data_dir`, and settings pinned for the seed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from farmcore.core import seed as core_seed


def _square(lon: float, lat: float, d: float = 0.002) -> dict:
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def _feature(id_parcel: str, alias: str, name: str, lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "id_parcel": id_parcel,
            "code_cultu": "VRC",
            "alias": alias,
            "name": name,
            "variety": "Merlot",
            "eppo_crop": "VITVI",
            "crop_label": "Grapevine (wine grapes)",
            "planted_year": 2001,
            "area_ha": 1.25,
        },
        "geometry": _square(lon, lat),
    }


# Deliberately out of order (P10 before P2) to prove the alias-number sort.
SYNTHETIC = {
    "type": "FeatureCollection",
    "features": [
        _feature("900010", "P10", "Synthetic Ten", -0.150, 44.890),
        _feature("900002", "P2", "Synthetic Two", -0.160, 44.895),
    ],
}


@pytest.fixture
def synthetic_data_dir(tmp_path: Path) -> Path:
    ign = tmp_path / "ign"
    ign.mkdir()
    (ign / "demo_parcels_named.geojson").write_text(json.dumps(SYNTHETIC))
    return tmp_path


@pytest.fixture(autouse=True)
def _seed_settings(settings, monkeypatch):
    """Seeds read `get_settings()`; pin it to the test settings (no `.env`)."""
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
