"""Drafts: every proposed write waits here until the farmer confirms it.

Imports, the Record sheet and external agents propose writes into one `draft` table. The kind
(`Manifest.draft_kinds`) belongs to an app: its validator checks the payload at propose and edit
time, its review fragment shows it on `/drafts/<id>`, and its confirm service turns it into a
record. "Draft" (not yet confirmed) is distinct from "planned" (planned work).

- **Scope.** A draft is visible only on its farm, and only to the principal that proposed it; a
  draft an agent proposed (`token:<id>`) is reviewed by the owner who issued that token. Anyone
  else gets `DraftNotFound` (a 404), so a numeric id alone grants nothing.
- **Confirm and cancel** run under the farm's operations lock (`logs.lock_farm`) and a row lock,
  so retried and concurrent confirms record once: the first confirms, the others read the
  confirmed draft and return its record. Cancel is idempotent; a finished draft never changes.
- **Revision.** Every edit bumps `revision`; a confirm names the revision the farmer reviewed,
  and an older one is refused (`DraftStale`).
- **Expiry** is wall time (as for grant validity), never the replay clock: a draft past
  `expires_at` cannot be confirmed whether or not the expiry job has marked it yet. A restored
  copy loads drafts as farm data and refuses the ones that expired meanwhile the same way; its
  expiry job then marks them.
- **Provenance** (file name and hash, registry reference, the proposing actor) is validated here
  and handed to the kind's confirm service, which keeps it on the record it writes.

Proposing through an agent token is gated by the caller on `drafts:propose` in the token
grant's scopes (`access.DRAFTS_PROPOSE`); this module takes the actor it is given.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from farmcore.core.logs import lock_farm
from farmcore.core.manifest import JobContext, Manifest, collect_draft_kinds, import_string
from farmcore.core.models.drafts import Draft
from farmcore.core.models.identity import AccessToken

# Product choice, `assumed`: an import or agent draft stays reviewable for a week.
DEFAULT_TTL = timedelta(days=7)
MAX_TTL = timedelta(days=30)
MAX_PAYLOAD_BYTES = 64_000


class DraftError(Exception):
    """A draft action the farmer cannot take; `reason` says why in plain words."""

    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DraftNotFound(DraftError):
    status_code = 404


class DraftInvalid(DraftError):
    status_code = 422


class DraftConflict(DraftError):
    """The request key already holds a different draft."""


class DraftClosed(DraftError):
    """The draft was already confirmed, cancelled or expired."""


class DraftExpired(DraftError):
    pass


class DraftStale(DraftError):
    pass


class Provenance(BaseModel):
    """Where a proposed write came from. Text in it is data, never instructions."""

    model_config = ConfigDict(extra="forbid")
    source_type: Literal["voice", "file", "isoxml"] = "file"
    file_name: str | None = Field(default=None, max_length=255)
    file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registry_ref: str | None = Field(default=None, max_length=200)
    details: dict[str, Any] = Field(default_factory=dict)


def wall_now() -> datetime:
    return datetime.now(UTC)


def _kind(manifests: Sequence[Manifest], key: str):
    found = collect_draft_kinds(manifests).get(key)
    if found is None:
        raise DraftInvalid(f"No enabled app records {key!r} drafts.")
    return found[1]


def _validated(session: Session, manifests, farm_id: int, kind: str, payload: dict) -> dict:
    validator = import_string(_kind(manifests, kind).validator)
    try:
        clean = validator(session, farm_id, dict(payload))
    except (ValueError, ValidationError) as exc:
        raise DraftInvalid(str(exc)) from exc
    if len(json.dumps(clean, default=str)) > MAX_PAYLOAD_BYTES:
        raise DraftInvalid("This draft is too large.")
    return clean


def propose(
    session: Session,
    manifests: Sequence[Manifest],
    *,
    farm_id: int,
    actor: str,
    kind: str,
    payload: dict[str, Any],
    provenance: dict[str, Any],
    request_key: str,
    ttl: timedelta = DEFAULT_TTL,
) -> tuple[Draft, bool]:
    """Validate and store a proposed write; returns `(draft, created)`.

    The same `request_key` from the same actor returns the draft it already holds when the
    payload and provenance match, and raises `DraftConflict` otherwise."""
    if not request_key.strip() or len(request_key) > 128:
        raise DraftInvalid("A draft needs a request key of at most 128 characters.")
    if not timedelta(0) < ttl <= MAX_TTL:
        raise DraftInvalid("A draft expires within 30 days.")
    try:
        source = Provenance.model_validate(provenance).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise DraftInvalid(str(exc)) from exc
    clean = _validated(session, manifests, farm_id, kind, payload)
    lock_farm(session, farm_id)
    held = session.scalar(
        select(Draft)
        .where(Draft.farm_id == farm_id, Draft.actor == actor, Draft.request_key == request_key)
        .execution_options(populate_existing=True)
    )
    if held is not None:
        if (held.kind, held.payload, held.provenance) != (kind, clean, source):
            raise DraftConflict("This request key already holds a different draft.")
        return held, False
    draft = Draft(
        farm_id=farm_id,
        actor=actor,
        kind=kind,
        request_key=request_key,
        payload=clean,
        provenance=source,
        status="open",
        revision=1,
        expires_at=wall_now() + ttl,
    )
    session.add(draft)
    session.flush()
    return draft, True


def reviewers(session: Session, draft: Draft) -> set[str]:
    """The actors who may open, confirm and cancel `draft`."""
    allowed = {draft.actor}
    kind, _, raw_id = draft.actor.partition(":")
    if kind == "token" and raw_id.isdigit():
        issuer = session.scalar(
            select(AccessToken.created_by_identity_id).where(AccessToken.id == int(raw_id))
        )
        if issuer is not None:
            allowed.add(f"identity:{issuer}")
    return allowed


def _load(
    session: Session, farm_id: int, draft_id: int, actor: str, *, lock: bool = False
) -> Draft:
    if not 1 <= draft_id < 2**63:
        raise DraftNotFound("Draft not found.")
    stmt = select(Draft).where(Draft.id == draft_id, Draft.farm_id == farm_id)
    if lock:
        lock_farm(session, farm_id)
        stmt = stmt.with_for_update()
    draft = session.scalar(stmt.execution_options(populate_existing=True))
    if draft is None or actor not in reviewers(session, draft):
        raise DraftNotFound("Draft not found.")
    return draft


def get(session: Session, *, farm_id: int, draft_id: int, actor: str) -> Draft:
    """The draft, if `actor` may review it on this farm; else `DraftNotFound`."""
    return _load(session, farm_id, draft_id, actor)


def is_expired(draft: Draft, at: datetime | None = None) -> bool:
    return draft.status == "expired" or (
        draft.status == "open" and draft.expires_at <= (at or wall_now())
    )


def refusal(draft: Draft, revision: int | None = None) -> DraftError | None:
    """Why `draft` cannot be confirmed now (at `revision`), or None."""
    if is_expired(draft):
        return DraftExpired(
            f"This draft expired on {draft.expires_at:%Y-%m-%d %H:%M} UTC; nothing was recorded. "
            "Propose it again."
        )
    if draft.status == "cancelled":
        return DraftClosed("This draft was cancelled; nothing was recorded.")
    if draft.status == "confirmed":
        return None
    if revision is not None and revision != draft.revision:
        return DraftStale(
            "This draft changed since you opened it; nothing was recorded. Review it again."
        )
    return None


def revise(
    session: Session,
    manifests: Sequence[Manifest],
    *,
    farm_id: int,
    draft_id: int,
    actor: str,
    payload: dict[str, Any],
    revision: int,
) -> Draft:
    """Replace an open draft's payload (validated again) and bump its revision."""
    draft = _load(session, farm_id, draft_id, actor, lock=True)
    if draft.status != "open":
        raise DraftClosed("Only an open draft can be edited.")
    problem = refusal(draft, revision)
    if problem is not None:
        raise problem
    draft.payload = _validated(session, manifests, farm_id, draft.kind, payload)
    draft.revision += 1
    session.flush()
    return draft


def confirm(
    session: Session,
    manifests: Sequence[Manifest],
    *,
    farm_id: int,
    draft_id: int,
    actor: str,
    revision: int,
    now: datetime,
) -> tuple[Draft, bool]:
    """Record the draft through its kind's confirm service; returns `(draft, recorded_now)`.

    A draft already confirmed returns `(draft, False)` with its record unchanged, so a retried
    or concurrent confirm never records twice. `now` is the farm clock, for the record."""
    draft = _load(session, farm_id, draft_id, actor, lock=True)
    if draft.status == "confirmed":
        return draft, False
    problem = refusal(draft, revision)
    if problem is not None:
        raise problem
    service = import_string(_kind(manifests, draft.kind).confirm or "")
    try:
        model, record_id = service(session, draft, actor=actor, now=now)
    except ValueError as exc:
        raise DraftInvalid(str(exc)) from exc
    draft.status = "confirmed"
    draft.result_model, draft.result_id = model, record_id
    draft.finalized_at, draft.finalized_by = wall_now(), actor
    session.flush()
    return draft, True


def cancel(
    session: Session, *, farm_id: int, draft_id: int, actor: str, now: datetime
) -> tuple[Draft, bool]:
    """Discard an open draft; returns `(draft, cancelled_now)`. Cancelling twice is a no-op."""
    draft = _load(session, farm_id, draft_id, actor, lock=True)
    if draft.status == "cancelled":
        return draft, False
    if draft.status != "open":
        raise DraftClosed(f"This draft is already {draft.status}.")
    draft.status = "cancelled"
    draft.finalized_at, draft.finalized_by = wall_now(), actor
    session.flush()
    return draft, True


def expire_due(
    session: Session, *, farm_ids: Sequence[int] | None = None, at: datetime | None = None
) -> int:
    """Mark open drafts past their wall-clock expiry as expired; returns how many."""
    at = at or wall_now()
    stmt = (
        update(Draft)
        .where(Draft.status == "open", Draft.expires_at <= at)
        .values(status="expired", finalized_at=at)
    )
    if farm_ids is not None:
        stmt = stmt.where(Draft.farm_id.in_(list(farm_ids)))
    return session.execute(stmt.execution_options(synchronize_session=False)).rowcount


def expire_job(ctx: JobContext) -> None:
    """Manifest job `drafts.expire`: wall time, whatever the replay clock says."""
    expire_due(ctx.session, farm_ids=ctx.farm_ids)
