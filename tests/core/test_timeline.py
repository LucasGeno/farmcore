"""Timeline: post_event and the parcel timeline (newest first by occurred_at)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import AccessLog, Farm, Parcel
from farmcore.core.timeline import parcel_timeline, post_event

T0 = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"


def _farm_parcel(session, name: str) -> tuple[Farm, Parcel]:
    farm = Farm(name=name)
    session.add(farm)
    session.flush()
    parcel = Parcel(farm_id=farm.id, name="P1", alias="P1", area_ha=Decimal("1"), geom=SQUARE)
    session.add(parcel)
    session.flush()
    return farm, parcel


def _post(session, farm, parcel, kind: str, at: datetime, body: str = "x"):
    return post_event(
        session,
        farm_id=farm.id,
        res_model="parcel",
        res_id=parcel.id,
        kind=kind,
        body=body,
        actor="owner",
        source_app="operations",
        occurred_at=at,
    )


def _repo(session, farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "web")


def test_post_event_stores_every_field(session):
    farm, parcel = _farm_parcel(session, "A")

    event = post_event(
        session,
        farm_id=farm.id,
        res_model="parcel",
        res_id=parcel.id,
        kind="log.created",
        body="Spray logged",
        payload={"log_id": 7},
        actor="owner",
        source_app="chat",
        occurred_at=T0,
    )

    assert event.id is not None
    assert (event.kind, event.body, event.payload, event.actor, event.source_app) == (
        "log.created",
        "Spray logged",
        {"log_id": 7},
        "owner",
        "chat",
    )
    assert event.occurred_at == T0


def test_timeline_is_newest_first_by_occurred_at(session):
    farm, parcel = _farm_parcel(session, "A")
    _post(session, farm, parcel, "log.created", T0 + timedelta(hours=2), "middle")
    _post(session, farm, parcel, "advice.evaluated", T0 + timedelta(hours=5), "newest")
    _post(session, farm, parcel, "log.confirmed", T0, "oldest")

    events = parcel_timeline(_repo(session, farm), parcel.id)

    assert [e.body for e in events] == ["newest", "middle", "oldest"]


def test_timeline_limit_until_and_other_parcels(session):
    farm, parcel = _farm_parcel(session, "A")
    other_farm, other_parcel = _farm_parcel(session, "B")
    for h in range(4):
        _post(session, farm, parcel, "advice.evaluated", T0 + timedelta(hours=h), f"h{h}")
    _post(session, other_farm, other_parcel, "log.created", T0, "other")
    repo = _repo(session, farm)

    assert [e.body for e in parcel_timeline(repo, parcel.id, limit=2)] == ["h3", "h2"]
    assert [e.body for e in parcel_timeline(repo, parcel.id, until=T0 + timedelta(hours=1))] == [
        "h1",
        "h0",
    ]
    assert parcel_timeline(repo, other_parcel.id) == []


def test_timeline_read_writes_one_access_log_row(session):
    farm, parcel = _farm_parcel(session, "A")

    parcel_timeline(_repo(session, farm), parcel.id)

    rows = session.scalars(select(AccessLog)).all()
    assert [(r.resource, r.scope) for r in rows] == [("event", "logs:read")]


@pytest.mark.parametrize("kind", ["created", "Log.created", "log.", "log.created.x"])
def test_post_event_rejects_malformed_kinds(session, kind):
    farm, parcel = _farm_parcel(session, "A")

    with pytest.raises(ValueError, match="kind"):
        _post(session, farm, parcel, kind, T0)


def test_post_event_rejects_naive_occurred_at(session):
    farm, parcel = _farm_parcel(session, "A")

    with pytest.raises(ValueError, match="aware"):
        _post(session, farm, parcel, "log.created", datetime(2024, 4, 10, 6, 0))
