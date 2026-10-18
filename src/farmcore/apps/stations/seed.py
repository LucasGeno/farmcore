"""Stations seed: the St-Émilion Météo-France station (33394002) on the demo farm.

Idempotent through ext_id `meteofrance`:`33394002`; a reseed keeps the connector health the
ingest job wrote into `props`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from farmcore.apps.stations import connector
from farmcore.core import ext_ids
from farmcore.core.models.canonical import Asset
from farmcore.core.seed import ensure_default_farm

STATION_NAME = "St-Émilion 33394002"
LON, LAT, ALTI_M = -0.188, 44.917667, 38


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    farm = ensure_default_farm(session)
    ext_ids.ensure_source(
        session,
        connector.SOURCE_CODE,
        "Météo-France hourly observations, station 33394002 (St-Émilion)",
        "weather",
        "Licence Ouverte",
    )
    existing = ext_ids.lookup(session, connector.EXT_SOURCE, connector.STATION_KEY)
    props = dict(existing.props) if isinstance(existing, Asset) else {}
    props.update({"alti_m": ALTI_M, "file": connector.FILE})
    ext_ids.upsert(
        session,
        source=connector.EXT_SOURCE,
        key=connector.STATION_KEY,
        model=Asset,
        values={
            "farm_id": farm.id,
            "kind": "station",
            "name": STATION_NAME,
            "geom": f"SRID=4326;POINT({LON} {LAT})",
            "props": props,
        },
    )
    return {"station": 1}
