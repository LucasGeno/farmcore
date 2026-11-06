"""Human opinions about one exact stored assessment, kept as append-only events.

A review request and its answer are events on the saved advice row (`res_model="advice"`), not
new advice state: the model output is never edited and a review never follows a later run. The
event log is already the farm's append-only audit trail, so no new table is needed.

Reads go through `repo.execute(..., scope="advice:read", resource="recommendation_review")`:
the note is advice-review material, so a consent limited to records (`logs:read`) cannot read it
by naming the generic `event` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Advice, Event, Parcel
from farmcore.core.timeline import post_event

REQUEST_KIND = "advice.review_requested"
RESPONSE_KIND = "advice.reviewed"
KINDS = (REQUEST_KIND, RESPONSE_KIND)
SOURCE_APP = "review"
SCOPE = "advice:read"
RESOURCE = "recommendation_review"
OPINIONS = ("agree", "disagree", "unsure")
OPINION_WORDS = {
    "agree": "Agrees with this saved check",
    "disagree": "Disagrees with this saved check",
    "unsure": "Unsure about this saved check",
}


class ReviewNotFound(LookupError):
    """The assessment or request is not on this farm (or not stored up to the clock)."""


class ReviewConflict(RuntimeError):
    """The request already awaits an answer, or this request was already answered."""


@dataclass(frozen=True)
class ReviewState:
    advice_id: int
    request: Event | None
    response: Event | None

    @property
    def open(self) -> bool:
        return self.request is not None and self.response is None


def _events(repo: ScopedRepository, advice_id: int | None = None) -> list[Event]:
    where = [
        Event.farm_id == repo.farm_id,
        Event.res_model == "advice",
        Event.kind.in_(KINDS),
    ]
    if advice_id is not None:
        where.append(Event.res_id == advice_id)
    stmt = select(Event).where(*where).order_by(Event.id)
    return list(repo.execute(stmt, scope=SCOPE, resource=RESOURCE).scalars())


def _state_from(events: list[Event], advice_id: int) -> ReviewState:
    requests = [e for e in events if e.kind == REQUEST_KIND and e.res_id == advice_id]
    if not requests:
        return ReviewState(advice_id, None, None)
    request = requests[-1]
    answered = [
        e
        for e in events
        if e.kind == RESPONSE_KIND
        and e.res_id == advice_id
        and (e.payload or {}).get("request_id") == request.id
    ]
    return ReviewState(advice_id, request, answered[-1] if answered else None)


def state(repo: ScopedRepository, advice_id: int) -> ReviewState:
    return _state_from(_events(repo, advice_id), advice_id)


def states(repo: ScopedRepository) -> list[ReviewState]:
    """Every reviewed or requested assessment on this farm, newest request first."""
    events = _events(repo)
    found = [_state_from(events, advice_id) for advice_id in {e.res_id for e in events}]
    return sorted(
        (state for state in found if state.request is not None),
        key=lambda state: state.request.id,
        reverse=True,
    )


def request(repo: ScopedRepository, request_id: int) -> Event:
    stmt = select(Event).where(
        Event.farm_id == repo.farm_id,
        Event.id == request_id,
        Event.res_model == "advice",
        Event.kind == REQUEST_KIND,
    )
    event = repo.execute(stmt, scope=SCOPE, resource=RESOURCE).scalar_one_or_none()
    if event is None:
        raise ReviewNotFound("Advisor check unavailable")
    return event


def request_check(
    repo: ScopedRepository,
    now: datetime,
    advice: Advice,
    parcel: Parcel,
    *,
    note: str,
    requested_by: str,
) -> ReviewState:
    """Ask an assigned advisor about this exact saved row; one open request at a time."""
    current = state(repo, advice.id)
    if current.open:
        raise ReviewConflict("This saved check already awaits an advisor")
    event = post_event(
        repo.session,
        farm_id=repo.farm_id,
        res_model="advice",
        res_id=advice.id,
        kind=REQUEST_KIND,
        body=f"Advisor check requested for {parcel.alias}",
        payload={
            "advice_id": advice.id,
            "parcel_id": parcel.id,
            "alias": parcel.alias,
            "name": parcel.name,
            "evaluated_at": advice.evaluated_at.isoformat(),
            "decision": advice.decision,
            "assessment_status": (advice.props or {}).get("assessment_status", "legacy-unknown"),
            "headline": (advice.props or {}).get("headline"),
            "note": note,
            "requested_by": requested_by,
        },
        actor=repo.actor,
        source_app=SOURCE_APP,
        occurred_at=now,
    )
    return ReviewState(advice.id, event, None)


def respond(
    repo: ScopedRepository,
    now: datetime,
    advice: Advice,
    parcel: Parcel,
    request_id: int,
    *,
    opinion: str,
    note: str,
    reviewer: str,
) -> ReviewState:
    """The assigned advisor's opinion and note on one request; an answer is final."""
    asked = request(repo, request_id)
    if asked.res_id != advice.id:
        raise ReviewNotFound("Advisor check unavailable")
    answered = any(
        event.kind == RESPONSE_KIND and (event.payload or {}).get("request_id") == asked.id
        for event in _events(repo, advice.id)
    )
    if answered:
        raise ReviewConflict("This advisor check already has an answer")
    event = post_event(
        repo.session,
        farm_id=repo.farm_id,
        res_model="advice",
        res_id=advice.id,
        kind=RESPONSE_KIND,
        body=f"Advisor check answered for {parcel.alias}",
        payload={
            "request_id": asked.id,
            "advice_id": advice.id,
            "parcel_id": parcel.id,
            "opinion": opinion,
            "note": note,
            "reviewer": reviewer,
        },
        actor=repo.actor,
        source_app=SOURCE_APP,
        occurred_at=now,
    )
    return ReviewState(advice.id, asked, event)
