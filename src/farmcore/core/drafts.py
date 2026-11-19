"""Drafts: every proposed write waits here until the farmer confirms it.

Imports, the Record sheet and external agents propose writes into one `draft` table. The kind
(`Manifest.draft_kinds`) belongs to an app: its validator checks the payload at propose and edit
time, its review fragment shows it on `/drafts/<id>`, and its confirm service turns it into a
record. "Draft" (not yet confirmed) is distinct from "planned" (planned work).

- **Scope.** A draft is visible only on its farm, and only to the principal that proposed it; a
  draft an agent proposed (`token:<id>`) is reviewed by the owner who issued that token, stored on
  the draft at propose time (`reviewer_identity_id`; the token's grant must be on the draft's
  farm). A Telegram chat's draft (`chat:<id>`) is also reviewed on the web by the owners of the
  farm the chat is linked to (`/start` links chats to the installation's farm, the draft's
  farm): the operator, and any member whose owner membership is active, checked live at each
  review so a revoked or disabled owner loses it at once.
  Anyone else gets `DraftNotFound` (a 404), so a numeric id alone grants nothing.
- **Agents only propose.** A `token:<id>` actor may read its own draft but never confirm, cancel
  or edit it (`DraftForbidden`): confirmation is the farmer's action in FarmCore.
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

from farmcore.core import registry
from farmcore.core.logs import lock_farm
from farmcore.core.manifest import JobContext, Manifest, collect_draft_kinds, import_string
from farmcore.core.models.canonical import DataGrant
from farmcore.core.models.drafts import Draft
from farmcore.core.models.identity import AccessToken, FarmMembership, LocalIdentity

# Product choice, `assumed` (open decision): an import or agent draft stays reviewable a week.
DEFAULT_TTL = timedelta(days=7)
MAX_TTL = timedelta(days=30)
# Provenance is copied onto the confirmed record's `log_source`; keep it a receipt, not a file.
MAX_PROVENANCE_BYTES = 8_000
TOKEN = "token"
CHAT = "chat"
OPERATOR = "operator"


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


class DraftForbidden(DraftError):
    """An agent token proposes drafts; only a person confirms, cancels or edits them."""

    status_code = 403


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
    # manual: typed by a farmer; agent: proposed by an external agent through its token.
    source_type: Literal["voice", "file", "isoxml", "manual", "agent", "registry"]
    file_name: str | None = Field(default=None, max_length=255)
    file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registry_ref: str | None = Field(default=None, max_length=200)
    details: dict[str, Any] = Field(default_factory=dict)


def wall_now() -> datetime:
    return datetime.now(UTC)


def _kind(session: Session, manifests: Sequence[Manifest], key: str):
    """The draft kind `key` of an app that is on now (`registry.live_active`), whichever process
    asks: a switched-off app's drafts can be neither proposed, edited nor confirmed."""
    found = collect_draft_kinds(registry.live_active(session, manifests)).get(key)
    if found is None:
        raise DraftInvalid(f"No enabled app records {key!r} drafts.")
    return found[1]


def validation_reason(error: ValueError) -> str:
    """Short validation feedback without model names, input echoes or library URLs."""
    if isinstance(error, ValidationError):
        issue = error.errors(include_url=False, include_input=False)[0]
        field = ".".join(str(part) for part in issue["loc"])[:80] or "draft"
        reason = {"extra_forbidden": "unexpected field", "missing": "required field"}.get(
            issue["type"], "invalid value"
        )
        return f"{field}: {reason}."
    reason = str(error)
    # Domain validators raise short sentences. Native parser exceptions are not
    # field guidance and must not echo the raw conversion input.
    if (
        "\n" in reason
        or len(reason) > 180
        or reason.startswith(
            ("invalid literal", "could not convert", "not enough values", "too many values")
        )
    ):
        return "draft: invalid value."
    return reason or "draft: invalid value."


def _validated(session: Session, manifests, farm_id: int, kind: str, payload: dict) -> dict:
    definition = _kind(session, manifests, kind)
    validator = import_string(definition.validator)
    try:
        clean = validator(session, farm_id, dict(payload))
    except (ValueError, ValidationError) as exc:
        raise DraftInvalid(validation_reason(exc)) from exc
    if len(json.dumps(clean, default=str).encode("utf-8")) > definition.max_payload_bytes:
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
        raise DraftInvalid(validation_reason(exc)) from exc
    if len(json.dumps(source)) > MAX_PROVENANCE_BYTES:
        raise DraftInvalid("This draft's provenance is too large.")
    reviewer = _token_issuer(session, farm_id, actor)
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
        reviewer_identity_id=reviewer,
        payload=clean,
        provenance=source,
        status="open",
        revision=1,
        expires_at=wall_now() + ttl,
    )
    session.add(draft)
    session.flush()
    return draft, True


def _token_issuer(session: Session, farm_id: int, actor: str) -> int | None:
    """The identity that issued a `token:<id>` actor's token; None for any other actor.

    The token's grant must be on `farm_id`: a token of another farm never stamps a reviewer here.
    """
    kind, _, raw_id = actor.partition(":")
    if kind != TOKEN:
        return None
    row = None
    if raw_id.isdigit() and int(raw_id) < 2**63:
        row = session.execute(
            select(AccessToken.created_by_identity_id, DataGrant.farm_id)
            .join(DataGrant, DataGrant.id == AccessToken.grant_id)
            .where(AccessToken.id == int(raw_id))
        ).first()
    if row is None or row.farm_id != farm_id:
        raise DraftInvalid("This agent token is not for this farm.")
    return row.created_by_identity_id


def reviewers(draft: Draft) -> set[str]:
    """The actors who may open `draft` (a token only reads; see `_load`)."""
    allowed = {draft.actor}
    if draft.actor.partition(":")[0] == CHAT:
        allowed.add(OPERATOR)
    if draft.reviewer_identity_id is not None:
        allowed.add(f"identity:{draft.reviewer_identity_id}")
    return allowed


def _farm_owner(session: Session, farm_id: int, actor: str) -> bool:
    """Whether `actor` is `identity:<id>` with an active owner membership on the farm (the
    rule of `access.require_farm_owner`)."""
    kind, _, raw_id = actor.partition(":")
    if kind != "identity" or not raw_id.isdigit() or int(raw_id) >= 2**63:
        return False
    return (
        session.scalar(
            select(FarmMembership.id)
            .join(LocalIdentity)
            .where(
                FarmMembership.farm_id == farm_id,
                FarmMembership.identity_id == int(raw_id),
                FarmMembership.role == "owner",
                FarmMembership.revoked_at.is_(None),
                LocalIdentity.disabled_at.is_(None),
            )
        )
        is not None
    )


def may_review(session: Session, draft: Draft, actor: str) -> bool:
    """Whether `actor` may open `draft`: a stored reviewer, or an owner of a chat draft's farm."""
    if actor in reviewers(draft):
        return True
    return draft.actor.partition(":")[0] == CHAT and _farm_owner(session, draft.farm_id, actor)


def _load(
    session: Session, farm_id: int, draft_id: int, actor: str, *, lock: bool = False
) -> Draft:
    """The draft `actor` may review; `lock` (confirm, cancel, edit) also refuses agent tokens."""
    if not 1 <= draft_id < 2**63:
        raise DraftNotFound("Draft not found.")
    stmt = select(Draft).where(Draft.id == draft_id, Draft.farm_id == farm_id)
    if lock:
        if actor.partition(":")[0] == TOKEN:
            raise DraftForbidden("An agent proposes drafts; the farmer confirms them.")
        lock_farm(session, farm_id)
        stmt = stmt.with_for_update()
    draft = session.scalar(stmt.execution_options(populate_existing=True))
    if draft is None or not may_review(session, draft, actor):
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
    ttl: timedelta | None = None,
) -> Draft:
    """Replace an open draft's payload (validated again) and bump its revision; `ttl` also
    restarts its expiry (a conversation's draft lives a while after its last turn)."""
    draft = _load(session, farm_id, draft_id, actor, lock=True)
    if draft.status != "open":
        raise DraftClosed("Only an open draft can be edited.")
    problem = refusal(draft, revision)
    if problem is not None:
        raise problem
    draft.payload = _validated(session, manifests, farm_id, draft.kind, payload)
    draft.revision += 1
    if ttl is not None:
        if not timedelta(0) < ttl <= MAX_TTL:
            raise DraftInvalid("A draft expires within 30 days.")
        draft.expires_at = wall_now() + ttl
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
    services: dict[str, Any] | None = None,
) -> tuple[Draft, bool]:
    """Record the draft through its kind's confirm service; returns `(draft, recorded_now)`.

    A draft already confirmed returns `(draft, False)` with its record unchanged, so a retried
    or concurrent confirm never records twice. `now` is the farm clock, for the record;
    `services` (the process's composed readers, e.g. saved spray weather) go to the kind."""
    draft = _load(session, farm_id, draft_id, actor, lock=True)
    if draft.status == "confirmed":
        return draft, False
    problem = refusal(draft, revision)
    if problem is not None:
        raise problem
    service = import_string(_kind(session, manifests, draft.kind).confirm or "")
    try:
        model, record_id = service(session, draft, actor=actor, now=now, services=services or {})
    except ValueError as exc:
        raise DraftInvalid(validation_reason(exc)) from exc
    draft.status = "confirmed"
    draft.result_model, draft.result_id = model, record_id
    draft.finalized_at, draft.finalized_by = wall_now(), actor
    session.flush()
    return draft, True


def cancel(session: Session, *, farm_id: int, draft_id: int, actor: str) -> tuple[Draft, bool]:
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
