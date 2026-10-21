"""Explicit current-input prerequisite for tests concerned with historical rendering."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert

from farmcore.core.models.canonical import Asset, Observation


def complete_station(session, farm_id, now):
    station = Asset(farm_id=farm_id, kind="station", name="Synthetic complete station", props={})
    session.add(station)
    session.flush()
    ts = datetime(now.year, 3, 1, tzinfo=UTC)
    values = []
    while ts <= now:
        for variable, value in (("rr1_mm", 0.0), ("t_c", 15.0), ("rh_pct", 70.0), ("ff_kmh", 5.0)):
            values.append({"asset_id": station.id, "ts": ts, "variable": variable, "value": value})
        ts += timedelta(hours=1)
    session.execute(insert(Observation), values)
    return station
