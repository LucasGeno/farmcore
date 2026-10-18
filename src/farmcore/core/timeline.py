"""Timeline: one `event` row per thing that happened to a record, read newest first.

Kinds are `<area>.<verb>`: `log.created`, `log.confirmed`, `log.merged`, `log.cancelled`,
`advice.evaluated`, `advice.changed`. `occurred_at` is clock time (replay-aware), so the
timeline orders and bounds by it; `created_at` is wall time.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Event

KIND_RE = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
DEFAULT_LIMIT = 50


def post_event(
    session: Session,
    *,
    farm_id: int,
    res_model: str,
    res_id: int,
    kind: str,
    body: str,
    payload: dict[str, Any] | None = None,
    actor: str,
    source_app: str,
    occurred_at: datetime,
) -> Event:
    if not KIND_RE.match(kind):
        raise ValueError(f"event kind must be '<area>.<verb>', got {kind!r}")
    if occurred_at.tzinfo is None:
        raise ValueError(f"occurred_at must be timezone-aware, got {occurred_at!r}")
    event = Event(
        farm_id=farm_id,
        res_model=res_model,
        res_id=res_id,
        kind=kind,
        body=body,
        payload=payload or {},
        actor=actor,
        source_app=source_app,
        occurred_at=occurred_at,
    )
    session.add(event)
    session.flush()
    return event


def parcel_timeline(
    repo: ScopedRepository,
    parcel_id: int,
    limit: int = DEFAULT_LIMIT,
    *,
    until: datetime | None = None,
) -> list[Event]:
    """The parcel's events, newest first by `occurred_at`; `until` (clock now) hides later ones."""
    where = [Event.res_model == "parcel", Event.res_id == parcel_id]
    if until is not None:
        where.append(Event.occurred_at <= until)
    return repo.list(
        Event,
        *where,
        scope="logs:read",
        order_by=(Event.occurred_at.desc(), Event.id.desc()),
        limit=limit,
    )
