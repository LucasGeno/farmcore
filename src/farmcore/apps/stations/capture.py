"""Exact-slot station reading for saved spray weather: primitive station facts only.

The farm's station is the existing one (`service.station_for_farm`, first by id); the reading is
the stored hourly row at exactly `slot` (`service.load_hourly(slot, slot)`), never an older hour
and never a network fetch. Operations validates and stores what this returns; this module knows
nothing about logs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from farmcore.apps.stations import service
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import ExtId, Source

FACTS = ("t_c", "ff_kmh", "rr1_mm")  # temperature, wind, hour-ending rain


def provider(repo: ScopedRepository, station_id: int) -> dict[str, Any] | None:
    """The station's public provider reference: its ext id (`meteofrance` / `33394002`) and the
    data source's name and licence (the connector's `<source>-<key>` code), or None. Never the
    import file path."""
    stmt = select(ExtId).where(ExtId.model == "asset", ExtId.res_id == station_id)
    ext = repo.execute(stmt.order_by(ExtId.id), scope=service.SCOPE, resource="ext_id")
    ext = ext.scalars().first()
    if ext is None:
        return None
    code = f"{ext.source}-{ext.key}"
    source = next(iter(repo.list(Source, Source.code == code, scope=service.SCOPE)), None)
    return {
        "source": ext.source,
        "key": ext.key,
        "name": source.name if source else None,
        "licence": source.licence if source else None,
    }


def read_slot(repo: ScopedRepository, slot: datetime) -> dict[str, Any]:
    """`{"station": None}` without a station; else `{"station": {id, name, provider}, "reading":
    None | {ts, t_c, ff_kmh, rr1_mm}}`. A variable missing that hour is None, never 0."""
    station = service.station_for_farm(repo)
    if station is None:
        return {"station": None}
    rows = service.load_hourly(repo, station.id, slot, slot)
    reading = None
    if rows:
        row = rows[0]
        reading = {"ts": row["ts"], **{name: row.get(name) for name in FACTS}}
    identity = {"id": station.id, "name": station.name, "provider": provider(repo, station.id)}
    return {"station": identity, "reading": reading}
