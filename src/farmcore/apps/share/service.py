"""Share service: create and revoke third-party grants, their status, the grant-bound repository.

Consent validity is real-world (wall) time, as in `ScopedRepository.check`: `valid_from` is wall
now, status compares wall now, never the replay clock. The `share.*` events carry wall time too
(a grant happens in the real world, not in the replayed season).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from farmcore.apps.share import config
from farmcore.core.access import ALL_SCOPES, GRANT_READ_SCOPES, OWNER, ScopedRepository
from farmcore.core.actors import mask_actor
from farmcore.core.models.canonical import AccessLog, DataGrant
from farmcore.core.timeline import post_event

DEFAULT_TZ = "Europe/Paris"
DATE_FORMAT = "%d %b %Y"
SOURCE_APP = "share"
AUDIT_SCOPE = "audit:read"


def scope_words(scopes: list[str] | tuple[str, ...]) -> str:
    """`["logs:read", "register:read"]` -> `spray logs, spray register`."""
    return ", ".join(config.SCOPE_WORDS.get(s, s) for s in scopes)


def local_date(value: datetime, tz: str = DEFAULT_TZ) -> str:
    return value.astimezone(ZoneInfo(tz)).strftime(DATE_FORMAT)


def local_time(value: datetime, tz: str = DEFAULT_TZ) -> str:
    """`16 Oct 2026 at 14:02` in `tz`."""
    return value.astimezone(ZoneInfo(tz)).strftime(f"{DATE_FORMAT} at %H:%M")


def is_owner_grant(grant: DataGrant) -> bool:
    """The farmer's own grant (`owner`, or any grant holding `*`): never opened as a grantee."""
    return grant.grantee.casefold() == OWNER or ALL_SCOPES in (grant.scopes or [])


def is_shareable_grant(grant: DataGrant) -> bool:
    """Legacy broad, write, private-history and reserved owner grants fail closed as a whole."""
    return (
        bool(grant.scopes) and not is_owner_grant(grant) and set(grant.scopes) <= GRANT_READ_SCOPES
    )


def grant_repo(session: Session, grant: DataGrant, *, actor: str | None = None) -> ScopedRepository:
    """The grantee's repository: the grant's farm (never the farm switcher's), with
    the authenticated `user:<id>` (or legacy operator probe `grant:<id>`), with the grant's purpose.
    Every read is checked against the grant and logged with its farm and grant ID."""
    return ScopedRepository(
        session,
        grant.farm_id,
        grant,
        actor=actor or f"grant:{grant.id}",
        purpose=grant.purpose,
        grant_view=True,
        private_member=True,
    )


def grant_status(grant: DataGrant, now: datetime) -> str:
    """`revoked`, `scheduled` (not valid yet), `expired` or `active` at wall time `now`
    (the same order of checks as `ScopedRepository.check`)."""
    if grant.revoked_at is not None:
        return "revoked"
    if grant.valid_from is not None and grant.valid_from > now:
        return "scheduled"
    if grant.valid_to is not None and grant.valid_to <= now:
        return "expired"
    return "active"


class InvalidGrant(ValueError):
    """Grant input refused, in plain words; `field` names the form field it is about."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def create_grant(
    repo: ScopedRepository,
    *,
    grantee: str,
    recipient_identity_id: int | None = None,
    preset: str,
    purpose: str,
    valid_to: datetime,
    tz: str = DEFAULT_TZ,
) -> DataGrant:
    """A grant of `preset`'s scopes on `repo.farm_id` from wall now to `valid_to`, and its
    `share.granted` event. `InvalidGrant` (a `ValueError`) on invalid input; nothing is written
    then."""
    name = grantee.strip()
    if not name:
        raise InvalidGrant("grantee", "Give the third party a name.")
    if len(name) > config.GRANTEE_MAX_LEN:
        raise InvalidGrant(
            "grantee", f"The name is longer than {config.GRANTEE_MAX_LEN} characters."
        )
    if name.casefold() == OWNER:
        raise InvalidGrant("grantee", "The name 'owner' is reserved for the farmer's own access.")
    if preset not in config.PRESETS:
        raise InvalidGrant("preset", "Pick a preset from the list.")
    purpose = purpose.strip()
    if not purpose:
        raise InvalidGrant("purpose", "Say what the access is for (the purpose).")
    if len(purpose) > config.PURPOSE_MAX_LEN:
        raise InvalidGrant(
            "purpose", f"The purpose is longer than {config.PURPOSE_MAX_LEN} characters."
        )
    now = datetime.now(UTC)
    if valid_to.tzinfo is None:
        raise InvalidGrant("valid_to", "The end date needs a timezone.")
    if valid_to <= now:
        raise InvalidGrant("valid_to", "The end date must be in the future.")
    _label, scopes = config.PRESETS[preset]
    grant = DataGrant(
        farm_id=repo.farm_id,
        grantee=name,
        recipient_identity_id=recipient_identity_id,
        scopes=list(scopes),
        purpose=purpose,
        valid_from=now,
        valid_to=valid_to,
    )
    repo.session.add(grant)
    repo.session.flush()
    post_event(
        repo.session,
        farm_id=repo.farm_id,
        res_model="data_grant",
        res_id=grant.id,
        kind="share.granted",
        body=(
            f"Granted {name} read access: {scope_words(scopes)}, until {local_date(valid_to, tz)}"
        ),
        payload={"grantee": name, "scopes": list(scopes), "purpose": purpose},
        actor=repo.actor,
        source_app=SOURCE_APP,
        occurred_at=now,
    )
    return grant


def access_rows(
    repo: ScopedRepository,
    *,
    grantees_only: bool = False,
    limit: int = config.ACCESS_LOG_LIMIT,
    tz: str = DEFAULT_TZ,
) -> list[dict[str, Any]]:
    """The farm's last `limit` access_log rows, newest first (`audit:read`), each with its time
    (display tz), masked actor, purpose, resource, scope and grant (`#id grantee` or `owner`).
    `grantees_only` keeps rows of third-party grants."""
    grants = {g.id: g for g in repo.list(DataGrant, scope=AUDIT_SCOPE)}
    third = [gid for gid, g in grants.items() if not is_owner_grant(g)]
    where = [AccessLog.grant_id.in_(third)] if grantees_only else []
    rows = repo.list(
        AccessLog,
        *where,
        scope=AUDIT_SCOPE,
        order_by=(AccessLog.ts.desc(), AccessLog.id.desc()),
        limit=limit,
    )
    zone = ZoneInfo(tz)
    out = []
    for r in rows:
        g = grants.get(r.grant_id) if r.grant_id is not None else None
        out.append(
            {
                "id": r.id,
                "ts": r.ts.astimezone(zone).strftime("%d %b %Y %H:%M:%S"),
                "actor": mask_actor(r.actor),
                "purpose": r.purpose,
                "resource": r.resource,
                "scope": r.scope,
                "grant": "owner" if g is None or is_owner_grant(g) else f"#{g.id} {g.grantee}",
            }
        )
    return out


def revoke_grant(repo: ScopedRepository, grant_id: int, *, tz: str = DEFAULT_TZ) -> DataGrant:
    """Revoke a third-party grant of `repo.farm_id` at wall now and post `share.revoked`.
    Already revoked → unchanged, no second event. Owner grant, another farm's grant or an unknown
    id → `LookupError`."""
    grant = repo.session.get(DataGrant, grant_id)
    if grant is None or grant.farm_id != repo.farm_id or is_owner_grant(grant):
        raise LookupError(f"no third-party grant {grant_id} on this farm")
    if grant.revoked_at is not None:
        return grant
    now = datetime.now(UTC)
    grant.revoked_at = now
    repo.session.flush()
    post_event(
        repo.session,
        farm_id=repo.farm_id,
        res_model="data_grant",
        res_id=grant.id,
        kind="share.revoked",
        body=f"Revoked {grant.grantee}'s read access on {local_time(now, tz)}",
        payload={"grantee": grant.grantee},
        actor=repo.actor,
        source_app=SOURCE_APP,
        occurred_at=now,
    )
    return grant


def shared_advice(row) -> dict[str, Any]:
    """Stored advice projection; private parameter inputs/review evidence never travel here."""
    from farmcore.apps.advisor.service import advice_json

    stored = advice_json(row)
    allowed = (
        "id",
        "parcel_id",
        "evaluated_at",
        "decision",
        "headline",
        "reasons",
        "next_window",
        "protected_until",
        "mm_since_spray",
        "incubation_pct",
        "flags",
        "products",
        "passes",
        "treatments",
    )
    data = {key: stored[key] for key in allowed}
    props = row.props or {}
    data["assessment_status"] = props.get("assessment_status")
    version = props.get("config_version")
    data["config_version"] = None
    if isinstance(version, dict):
        safe = {
            key: version[key]
            for key in ("id", "hash", "scope", "status")
            if isinstance(version.get(key), (str, int))
        }
        safe["accuracy_claim"] = False
        review = version.get("review")
        if isinstance(review, dict):
            safe["review"] = {
                key: review[key]
                for key in ("id", "verdict", "status")
                if isinstance(review.get(key), (str, int))
            }
        data["config_version"] = safe
    return data
