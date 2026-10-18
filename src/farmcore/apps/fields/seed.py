"""Fields seed: the demo farm's vine parcels from the named IGN RPG 2024 GeoJSON.

Each feature becomes one parcel, upserted through ext_id `ign-rpg-2024`:`id_parcel`, so re-running
the seed changes nothing. Parcels are written in alias-number order (P1, P2, ..., P10).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from farmcore.core import ext_ids
from farmcore.core.models.canonical import Parcel
from farmcore.core.seed import ensure_default_farm

IGN_SOURCE = "ign-rpg-2024"
IGN_SOURCE_NAME = "IGN Registre Parcellaire Graphique 2024"
IGN_LICENCE = "Licence Ouverte 2.0"
PARCELS_FILE = Path("ign") / "demo_parcels_named.geojson"
PROP_KEYS = ("variety", "eppo_crop", "planted_year", "crop_label", "code_cultu")


def alias_number(alias: str) -> int:
    """`P10` -> 10, for natural parcel order; aliases without a number sort last."""
    match = re.search(r"\d+", alias)
    return int(match.group()) if match else 1_000_000


def _features(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / PARCELS_FILE
    if not path.is_file():
        raise FileNotFoundError(
            f"fields seed: parcel file not found: {path} "
            f"(expected {PARCELS_FILE} under data_dir; see sample-data/README.md)"
        )
    features = json.loads(path.read_text())["features"]
    return sorted(features, key=lambda f: alias_number(f["properties"]["alias"]))


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    farm = ensure_default_farm(session)
    ext_ids.ensure_source(session, IGN_SOURCE, IGN_SOURCE_NAME, "geodata", IGN_LICENCE)
    features = _features(data_dir)
    for feature in features:
        p = feature["properties"]
        id_parcel = str(p["id_parcel"])
        geom = func.ST_Multi(
            func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(feature["geometry"])), 4326)
        )
        ext_ids.upsert(
            session,
            source=IGN_SOURCE,
            key=id_parcel,
            model=Parcel,
            values={
                "farm_id": farm.id,
                "iacs_id": id_parcel,
                "alias": p["alias"],
                "name": p["name"],
                "area_ha": p["area_ha"],
                "geom": geom,
                "props": {k: p[k] for k in PROP_KEYS if k in p},
            },
        )
    return {"parcels": len(features)}
