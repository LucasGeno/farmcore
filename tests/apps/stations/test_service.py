"""Stations service: `station_for_farm`, `load_hourly` (pivoted, None for missing, ordered)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.stations.service import load_hourly, station_for_farm
from farmcore.core.access import ScopeDenied, ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import AccessLog, DataGrant, Farm, Observation

T0 = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)


def _repo(session) -> ScopedRepository:
    farm = session.scalar(select(Farm))
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")


def _add(session, asset_id: int, hour: int, **values: float) -> None:
    for variable, value in values.items():
        session.add(
            Observation(
                asset_id=asset_id, ts=T0 + timedelta(hours=hour), variable=variable, value=value
            )
        )
    session.flush()


def test_load_hourly_pivots_in_time_order_with_none_for_missing(session, seeded):
    seeded()
    repo = _repo(session)
    station = station_for_farm(repo)
    _add(session, station.id, 2, rr1_mm=0.0, t_c=12.5, rh_pct=95.0, ff_kmh=4.0)
    _add(session, station.id, 0, rr1_mm=1.2, t_c=10.0, rh_pct=88.0, ff_kmh=6.5)
    _add(session, station.id, 1, t_c=11.0, rh_pct=90.0)  # rain and wind missing
    _add(session, station.id, 5, t_c=9.0)  # outside the range

    rows = load_hourly(repo, station.id, T0, T0 + timedelta(hours=2))

    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    assert rows == [
        {"ts": T0, "rr1_mm": 1.2, "t_c": 10.0, "rh_pct": 88.0, "ff_kmh": 6.5},
        {"ts": t1, "rr1_mm": None, "t_c": 11.0, "rh_pct": 90.0, "ff_kmh": None},
        {"ts": t2, "rr1_mm": 0.0, "t_c": 12.5, "rh_pct": 95.0, "ff_kmh": 4.0},
    ]


def test_service_reads_are_scoped_and_logged(session, seeded):
    seeded()
    repo = _repo(session)
    station = station_for_farm(repo)
    load_hourly(repo, station.id, T0, T0 + timedelta(hours=1))

    rows = list(session.scalars(select(AccessLog).order_by(AccessLog.id)))
    assert [(r.resource, r.scope) for r in rows] == [
        ("asset", "stations:read"),
        ("observation", "stations:read"),
    ]


def test_a_grant_without_stations_scope_cannot_read_the_station(session, seeded):
    seeded()
    farm = session.scalar(select(Farm))
    grant = DataGrant(farm_id=farm.id, grantee="adviser", scopes=["logs:read"], purpose="x")
    session.add(grant)
    session.flush()

    with pytest.raises(ScopeDenied):
        station_for_farm(ScopedRepository(session, farm.id, grant, "adviser", "x"))
