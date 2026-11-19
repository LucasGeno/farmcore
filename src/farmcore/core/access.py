"""Scoped data access: every route, job and service reads through a `ScopedRepository`.

A repository is bound to one farm, one grant, an actor and a purpose. Each read:

1. checks the grant (same farm, not revoked, inside `valid_from`..`valid_to`) and that it
   holds `scope` (the owner grant holds `*`), else `ScopeDenied`;
2. checks that `scope` covers the resource (`RESOURCE_SCOPES`: observations need
   `stations:read`, so a `logs:read` grant cannot read them by naming its own scope), else
   `ScopeDenied`; every farm table must be declared by core or by the owning app manifest —
   an undeclared resource is denied;
3. filters rows to the farm: directly (`farm_id`), via `log` (quantity, log_source,
   log_parcel), via `asset` (observation), via `data_grant` (access_token) or via
   `weather_receipt` (weather observations and forecasts); a weather station is the farm's own
   or public (no owner); shared reference tables (product, source, catalog_item) are not
   farm-owned; any other table has no farm path and is read only through `execute`;
4. writes exactly one `access_log` row (actor, farm, grant, resource, scope, purpose).

`execute(stmt, scope, resource)` runs a caller-built statement as-is (the caller scopes it); it is
the only way to read farm-less tables such as `llm_audit` (`scope="audit:read"`).
Writes go through `repo.session`; with no per-user login, routes use the owner grant (`get_repo`).
Routes take `repo: Repo` (`Depends(get_repo, scope="function")`), so the request's writes and
access_log rows commit before the response is sent; a plain `Depends(get_repo)` fails at startup.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Collection, Iterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Select, or_, select
from sqlalchemy.engine import Result
from sqlalchemy.orm import Session

from farmcore.core import ext_ids
from farmcore.core.identity import token_hash
from farmcore.core.models.base import Base
from farmcore.core.models.canonical import (
    AccessLog,
    Asset,
    DataGrant,
    Event,
    Farm,
    Log,
    LogParcel,
    LogSource,
    Observation,
    Product,
    Quantity,
    Source,
)
from farmcore.core.models.catalog import CatalogItem, TypedQuantity
from farmcore.core.models.identity import AccessToken, FarmMembership, LocalIdentity
from farmcore.core.models.weather import (
    WeatherForecast,
    WeatherObservation,
    WeatherReceipt,
    WeatherStation,
)
from farmcore.db import get_session
from farmcore.settings import Settings

SCOPES = frozenset(
    {
        "fields:read",
        "stations:read",
        "logs:read",
        "logs:write",
        "corrections:read",
        "advice:read",
        "parameters:read",
        "planning:read",
        "planning:write",
        "register:read",
        "audit:read",
        "drafts:propose",
    }
)
GRANT_READ_SCOPES = frozenset(
    {"fields:read", "stations:read", "logs:read", "register:read", "advice:read", "planning:read"}
)
DRAFTS_PROPOSE = "drafts:propose"
# What an access token may hold: what an ordinary grant may read, plus proposing drafts the
# farmer confirms in FarmCore. Never `*`, a write scope or `audit:read` (it also covers the
# installation-wide model-call audit, which a farm owner cannot read either).
TOKEN_SCOPES = GRANT_READ_SCOPES | {DRAFTS_PROPOSE}
ALL_SCOPES = "*"
OWNER = "owner"
DEMO_SOURCE = "farmcore-demo"

# Rows owned by a farm through a parent: model -> (fk column, parent model).
_VIA_PARENT: dict[type[Base], tuple[Any, type[Base]]] = {
    Quantity: (Quantity.log_id, Log),
    TypedQuantity: (TypedQuantity.log_id, Log),
    LogSource: (LogSource.log_id, Log),
    LogParcel: (LogParcel.log_id, Log),
    Observation: (Observation.asset_id, Asset),
    AccessToken: (AccessToken.grant_id, DataGrant),
    WeatherObservation: (WeatherObservation.receipt_id, WeatherReceipt),
    WeatherForecast: (WeatherForecast.receipt_id, WeatherReceipt),
}
# Rows a farm owns or every farm shares (null owner: a public network station).
_OWNED_OR_PUBLIC: dict[type[Base], Any] = {WeatherStation: WeatherStation.owner_farm_id}
# Reference data shared by every farm (product catalogue, data sources).
_SHARED: frozenset[type[Base]] = frozenset({Product, Source, CatalogItem})

_ANY = SCOPES
_LOG_READS = frozenset({"logs:read", "logs:write", "register:read"})
# Which scopes may read which resource (table name). Every farm table must be declared here or
# by its app manifest (`Manifest.resource_scopes`); an undeclared resource is denied.
RESOURCE_SCOPES: dict[str, frozenset[str]] = {
    "access_log": frozenset({"audit:read"}),
    "access_token": frozenset({"audit:read"}),
    "advice": frozenset({"advice:read"}),
    "app_module": frozenset({"audit:read"}),
    "asset": frozenset({"stations:read"}),
    "catalog_item": _ANY,  # shared reference (a fertiliser's name), like `product`
    # The assessment envelope (`core/assessments.py`). Crop protection is its only kind today;
    # a per-kind read scope comes with the neutral core contract.
    "assessment": frozenset({"advice:read"}),
    "clock_state": frozenset({"audit:read"}),
    "crop_season": frozenset({"fields:read"}),
    "data_grant": frozenset({"audit:read"}),
    # Proposed writes (`core/drafts.py`): the owner reviews them, an agent token proposes them.
    # The farm filter alone shows every principal's drafts: a token's read goes through
    # `drafts.get`, which also filters by actor, never a bare `repo.list(Draft)`.
    "draft": frozenset({"logs:write", "drafts:propose"}),
    "event": frozenset({"logs:read", "advice:read"}),
    "ext_id": _ANY,  # shared reference; callers filter by model/res_id
    "farm": _ANY,
    "farm_membership": frozenset({"audit:read"}),
    "forecast_availability": frozenset({"fields:read", "planning:read", "planning:write"}),
    "llm_audit": frozenset({"audit:read"}),
    "local_identity": frozenset({"audit:read"}),
    "local_session": frozenset({"audit:read"}),
    "log": _LOG_READS,
    "log_parcel": _LOG_READS,
    "log_source": _LOG_READS,
    "login_bucket": frozenset({"audit:read"}),
    "observation": frozenset({"stations:read"}),
    "operation_correction": frozenset({"corrections:read"}),
    "parameter_activation": frozenset({"parameters:read"}),
    "parameter_review": frozenset({"parameters:read"}),
    "parameter_version": frozenset({"parameters:read"}),
    "parcel": frozenset({"fields:read", "register:read"}),
    "planning_job": frozenset({"planning:read", "planning:write"}),
    "planning_job_parcel": frozenset({"planning:read", "planning:write"}),
    "planning_job_revision": frozenset({"planning:read", "planning:write"}),
    "planning_machine": frozenset({"planning:read", "planning:write"}),
    "planning_operator": frozenset({"planning:read", "planning:write"}),
    "planning_resource_window": frozenset({"planning:read", "planning:write"}),
    "planning_job_assignment": frozenset({"planning:read", "planning:write"}),
    "product": _ANY,
    "quantity": _LOG_READS,
    # Recommendation-review events are advice material; a logs-only consent cannot read the
    # farmer's or advisor's note by naming the generic `event` table.
    "recommendation_review": frozenset({"advice:read"}),
    "source": _ANY,
    # Operational: which background task attempt is current per farm (core/tasks.py).
    "task_attempt": frozenset({"audit:read"}),
    "typed_quantity": _LOG_READS,
    "weather_forecast": frozenset({"stations:read"}),
    "weather_observation": frozenset({"stations:read"}),
    "weather_receipt": frozenset({"stations:read"}),
    "weather_station": frozenset({"stations:read"}),
}
RESOURCE_OWNERS: dict[str, str] = dict.fromkeys(RESOURCE_SCOPES, "core")


def register_resource_scopes(
    owner: str,
    declarations: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    owned: Collection[str],
) -> None:
    """Register an app manifest's resource -> scopes declarations (idempotent for equal sets).

    Called by the registry at discovery. An app may declare only tables it owns (`owned`), so it
    cannot claim another app's table or a legacy view. An identical re-declaration keeps the first
    owner so conflict messages keep naming the app that actually declared the table; conflicting
    redefinitions are refused so two apps cannot silently widen each other's access.
    """
    for resource, scopes in declarations:
        if not resource:
            raise ValueError(f"app {owner!r}: empty resource name")
        if resource not in owned:
            raise ValueError(f"app {owner!r}: resource {resource!r} is not one of its own tables")
        allowed = frozenset(scopes)
        if not allowed:
            raise ValueError(f"app {owner!r}: resource {resource!r} declares no scopes")
        unknown = allowed - SCOPES
        if unknown:
            raise ValueError(
                f"app {owner!r}: resource {resource!r} declares unknown scope(s) {sorted(unknown)}"
            )
        existing = RESOURCE_SCOPES.get(resource)
        if existing is not None:
            if existing != allowed:
                raise ValueError(
                    f"resource {resource!r} is already declared by {RESOURCE_OWNERS[resource]} "
                    f"with {sorted(existing)}; {owner!r} asked for {sorted(allowed)}"
                )
            # Identical re-declaration: keep the first owner so the conflict message keeps naming
            # the app that actually declared the table, and do not broaden the scope set.
            continue
        RESOURCE_SCOPES[resource] = allowed
        RESOURCE_OWNERS[resource] = owner


class ScopeDenied(PermissionError):
    """The grant does not allow this read."""


class FarmNotSeeded(LookupError):
    """The default farm does not exist yet (`farmcore seed` creates it)."""


class ScopedRepository:
    def __init__(
        self,
        session: Session,
        farm_id: int,
        grant: DataGrant,
        actor: str,
        purpose: str,
        *,
        private_member: bool = False,
        grant_view: bool = False,
    ) -> None:
        self.session = session
        self.farm_id = farm_id
        self.grant = grant
        self.actor = actor
        self.purpose = purpose
        self.private_member = private_member
        self.grant_view = grant_view

    def check(self, scope: str, resource: str | None = None) -> None:
        """Raise `ScopeDenied` unless the grant allows `scope` on this farm now and `scope`
        covers `resource`."""
        if self.grant_view and (
            not self.grant.scopes
            or not set(self.grant.scopes) <= TOKEN_SCOPES
            or scope not in GRANT_READ_SCOPES
            or (
                resource is not None
                and resource
                not in {
                    "farm",
                    "parcel",
                    "product",
                    "source",
                    "asset",
                    "observation",
                    "log",
                    "quantity",
                    "log_parcel",
                    "log_source",
                    "ext_id",
                    "advice",
                }
            )
        ):
            raise ScopeDenied("This resource is not available through ordinary grants.")
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; known: {sorted(SCOPES)}")
        if resource is not None:
            allowed = RESOURCE_SCOPES.get(resource)
            if allowed is None:
                raise ScopeDenied(
                    f"resource {resource!r} is not declared by core or any loaded app"
                )
            if scope not in allowed:
                raise ScopeDenied(
                    f"scope {scope!r} does not cover {resource!r}; use one of {sorted(allowed)}"
                )
        g = self.grant
        if g.farm_id != self.farm_id:
            raise ScopeDenied(f"grant {g.id} is for farm {g.farm_id}, not farm {self.farm_id}")
        if g.revoked_at is not None:
            raise ScopeDenied(f"grant {g.id} was revoked")
        # Consent validity is real-world (wall) time, not replay time.
        now = datetime.now(UTC)
        if g.valid_from is not None and g.valid_from > now:
            raise ScopeDenied(f"grant {g.id} is not valid yet")
        if g.valid_to is not None and g.valid_to <= now:
            raise ScopeDenied(f"grant {g.id} expired")
        if ALL_SCOPES not in g.scopes and scope not in g.scopes:
            raise ScopeDenied(f"grant {g.id} lacks scope {scope!r}")

    def list[M: Base](
        self,
        model: type[M],
        *where: Any,
        scope: str,
        order_by: Any = None,
        limit: int | None = None,
    ) -> list[M]:
        stmt = self._scoped(model).where(*where)
        if order_by is not None:
            stmt = stmt.order_by(
                *(order_by if isinstance(order_by, (list, tuple)) else (order_by,))
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        self._authorise(scope, model.__table__.name)
        return list(self.session.scalars(stmt))

    def get[M: Base](self, model: type[M], id: Any, *, scope: str) -> M | None:
        pk = model.__mapper__.primary_key
        if len(pk) != 1:
            raise TypeError(f"{model.__name__} has a composite key; use list()")
        stmt = self._scoped(model).where(pk[0] == id)
        self._authorise(scope, model.__table__.name)
        return self.session.scalar(stmt)

    def execute(self, stmt: Any, *, scope: str, resource: str) -> Result[Any]:
        """Run `stmt` unchanged (the caller filters it) after the scope check; logs `resource`."""
        self._authorise(scope, resource)
        return self.session.execute(stmt)

    def execute_unaudited(self, stmt: Any, *, scope: str, resource: str) -> Result[Any]:
        """`execute` without the access_log row: the scope and grant are still checked. Only for
        decorative reads of data the same viewer already reads through an audited path (the
        parcel silhouettes: the map's layer already logs the geometry read)."""
        self.check(scope, resource)
        return self.session.execute(stmt)

    def _scoped[M: Base](self, model: type[M]) -> Select[tuple[M]]:
        stmt = select(model)
        if model is Event and self.private_member:
            stmt = stmt.where(
                or_(
                    Event.kind != "log.created",
                    Event.payload["status"].as_string().is_distinct_from("pending"),
                ),
                Event.kind != "log.cancelled",
            )
        if model is Farm:
            return stmt.where(Farm.id == self.farm_id)
        if "farm_id" in model.__table__.c:
            return stmt.where(model.__table__.c.farm_id == self.farm_id)
        if model in _VIA_PARENT:
            fk, parent = _VIA_PARENT[model]
            return stmt.where(fk.in_(select(parent.id).where(parent.farm_id == self.farm_id)))
        if model in _OWNED_OR_PUBLIC:
            owner = _OWNED_OR_PUBLIC[model]
            return stmt.where(or_(owner == self.farm_id, owner.is_(None)))
        if model in _SHARED:
            return stmt
        raise TypeError(
            f"{model.__name__} has no farm path; "
            "read it with execute(stmt, scope=..., resource=...)"
        )

    def _authorise(self, scope: str, resource: str) -> None:
        self.check(scope, resource)
        self.session.add(
            AccessLog(
                actor=self.actor,
                farm_id=self.farm_id,
                grant_id=self.grant.id,
                resource=resource,
                scope=scope,
                purpose=self.purpose,
            )
        )
        self.session.flush()


def ensure_owner_grant(session: Session, farm: Farm) -> DataGrant:
    """The farm's live owner grant (`*` scopes), created if missing."""
    grant = session.scalar(
        select(DataGrant)
        .where(
            DataGrant.farm_id == farm.id,
            DataGrant.grantee == OWNER,
            DataGrant.revoked_at.is_(None),
        )
        .order_by(DataGrant.id)
        .limit(1)
    )
    if grant is None:
        if session.info.get("tour"):
            raise HTTPException(status_code=403, detail="Tour mode cannot create an owner grant.")
        grant = DataGrant(farm_id=farm.id, grantee=OWNER, scopes=[ALL_SCOPES], purpose=OWNER)
        session.add(grant)
        session.flush()
    return grant


def default_farm(session: Session, settings: Settings) -> Farm:
    """The demo farm, found by ext_id `farmcore-demo`:`settings.default_farm_key`."""
    farm = ext_ids.lookup(session, DEMO_SOURCE, settings.default_farm_key)
    if not isinstance(farm, Farm):
        raise FarmNotSeeded(
            f"default farm {DEMO_SOURCE}:{settings.default_farm_key} not found; run `farmcore seed`"
        )
    return farm


FARM_COOKIE = "farmcore_farm"
FARM_PARAM = "farm"


def _live_owner_grants() -> Select[tuple[DataGrant]]:
    # Consent validity is real-world (wall) time, not replay time (as in `check`).
    now = datetime.now(UTC)
    return select(DataGrant).where(
        DataGrant.grantee == OWNER,
        DataGrant.revoked_at.is_(None),
        DataGrant.valid_from.is_(None) | (DataGrant.valid_from <= now),
        DataGrant.valid_to.is_(None) | (DataGrant.valid_to > now),
    )


def owner_grant_live(session: Session, farm_id: int) -> DataGrant | None:
    """The farm's live owner grant (not revoked, inside its validity), or None."""
    return session.scalar(
        _live_owner_grants().where(DataGrant.farm_id == farm_id).order_by(DataGrant.id).limit(1)
    )


def owned_farms(session: Session) -> list[Farm]:
    """Farms with a live owner grant, by id (the farm switcher's list)."""
    granted = _live_owner_grants().with_only_columns(DataGrant.farm_id)
    return list(session.scalars(select(Farm).where(Farm.id.in_(granted)).order_by(Farm.id)))


def selectable_farm(session: Session, value: Any) -> Farm | None:
    """The farm `value` names (an id), if it exists and has a live owner grant."""
    try:
        farm_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    # Farm IDs are PostgreSQL bigints; ignore overflow before issuing a lookup.
    if not -(2**63) <= farm_id < 2**63:
        return None
    farm = session.get(Farm, farm_id)
    if farm is None or owner_grant_live(session, farm.id) is None:
        return None
    return farm


def selected_farm(request: Request, session: Session, settings: Settings) -> Farm:
    """The request's farm: `?farm=<id>`, else cookie `farmcore_farm`, else the default farm.
    An id that is not a farm with a live owner grant falls back to the default farm. Only web
    routes use it (`get_repo`); jobs, chat and the bot keep `default_farm`."""
    from farmcore.core.identity import principal_for
    from farmcore.web import operator_authenticated

    principal = principal_for(request)
    if principal is not None and not operator_authenticated(request):
        for value in (request.query_params.get(FARM_PARAM), request.cookies.get(FARM_COOKIE)):
            if value:
                try:
                    farm_id = int(value)
                except ValueError:
                    raise HTTPException(403, "Farm access denied.") from None
                if farm_id not in principal.memberships:
                    raise HTTPException(403, "Farm access denied.")
                farm = session.get(Farm, farm_id)
                if farm is None:
                    raise HTTPException(403, "Farm access denied.")
                return farm
        if not principal.memberships:
            raise HTTPException(403, "Farm access denied.")
        farm = session.get(Farm, min(principal.memberships))
        if farm is None:
            raise HTTPException(403, "Farm access denied.")
        return farm

    for value in (request.query_params.get(FARM_PARAM), request.cookies.get(FARM_COOKIE)):
        if value:
            if not operator_authenticated(request):
                try:
                    farm_id = int(value)
                except ValueError:
                    raise HTTPException(
                        status_code=403, detail="Authentication required."
                    ) from None
                authorize_farm_read(request, farm_id, settings)
            farm = selectable_farm(session, value)
            if farm is not None:
                authorize_farm_read(request, farm.id, settings)
                return farm
    farm = default_farm(session, settings)
    authorize_farm_read(request, farm.id, settings)
    return farm


def authorize_farm_read(request: Request, farm_id: int, settings: Settings) -> None:
    from farmcore.core.identity import principal_for
    from farmcore.web import operator_authenticated

    principal = principal_for(request)
    if principal is not None and farm_id in principal.memberships:
        return
    if not operator_authenticated(request) and (
        principal is not None or not settings.demo_mode or farm_id not in settings.demo_farm_ids
    ):
        raise HTTPException(status_code=403, detail="Authentication required.")


def get_repo(
    request: Request, session: Session = Depends(get_session, scope="function")
) -> Iterator[ScopedRepository]:
    """FastAPI dependency: owner repository on the selected farm (`selected_farm`: `?farm=`,
    cookie, default farm); commits when the route function returns, before the response is
    sent (so writes are visible to the next request and the access_log rows of a read are kept);
    rolls back if the route raises. Use it as `Repo`: the function-scoped session makes a
    request-scoped `Depends(get_repo)` fail at startup."""
    try:
        farm = selected_farm(request, session, request.app.state.settings)
    except FarmNotSeeded as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    request.state.selected_farm_id = farm.id
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        from farmcore.core.logs import lock_farm

        # Match canonical operation ordering: advisory lock, then farm row lock.
        lock_farm(session, farm.id)
        # Lock before owner-grant/audit inserts take farm foreign-key KEY SHARE locks.
        # Otherwise concurrent first writes can deadlock upgrading those locks.
        session.execute(select(Farm.id).where(Farm.id == farm.id).with_for_update())
    from farmcore.core.identity import principal_for
    from farmcore.web import operator_authenticated

    principal = principal_for(request)
    actor = f"user:{principal.identity_id}" if principal else OWNER
    repo = ScopedRepository(
        session,
        farm.id,
        ensure_owner_grant(session, farm),
        actor,
        "web",
        private_member=bool(principal and not operator_authenticated(request)),
    )
    try:
        yield repo
    except BaseException:
        session.rollback()
        raise
    session.commit()


def repo_for_job(session: Session, farm_id: int, job_name: str) -> ScopedRepository:
    """Owner repository for a job: actor `job:<name>`, purpose `job`. The job runner commits."""
    farm = session.get(Farm, farm_id)
    if farm is None:
        raise LookupError(f"farm {farm_id} not found")
    return ScopedRepository(
        session, farm_id, ensure_owner_grant(session, farm), f"job:{job_name}", "job"
    )


Repo = Annotated[ScopedRepository, Depends(get_repo, scope="function")]


# --- Access tokens ------------------------------------------------------------------------------
# A token is `<prefix><43 url-safe chars>` from 32 random bytes. Only its SHA-256 is stored: a
# slow KDF protects guessable secrets such as passwords, and 256 random bits are not guessable.
TOKEN_PREFIXES = {"personal": "fc_pat_"}
TOKEN_BYTES = 32
TOKEN_MAX_LEN = 128  # longer input is refused before hashing


class TokenRejected(PermissionError):
    """Unknown, malformed, revoked or expired token, wrong farm, or an issuer who lost
    authority. One message for every cause, and never the token."""

    def __init__(self) -> None:
        super().__init__("Invalid, expired or revoked token.")


def issue_token(
    session: Session, grant: DataGrant, *, kind: str, created_by: int
) -> tuple[AccessToken, str]:
    """A new token for `grant`: (its row, the token). The token exists only in the return value;
    show it once and never store, log or put it in a URL."""
    raw = TOKEN_PREFIXES[kind] + secrets.token_urlsafe(TOKEN_BYTES)
    token = AccessToken(
        token_hash=token_hash(raw), kind=kind, grant_id=grant.id, created_by_identity_id=created_by
    )
    session.add(token)
    session.flush()
    return token, raw


def is_farm_owner(session: Session, identity_id: int, farm_id: int) -> bool:
    """Whether `identity_id` is an enabled identity with a current owner membership of `farm_id`."""
    return (
        session.scalar(
            select(FarmMembership.id)
            .join(LocalIdentity)
            .where(
                FarmMembership.farm_id == farm_id,
                FarmMembership.identity_id == identity_id,
                FarmMembership.role == "owner",
                FarmMembership.revoked_at.is_(None),
                LocalIdentity.disabled_at.is_(None),
            )
        )
        is not None
    )


def authenticate_token(
    session: Session, raw: str, *, farm_id: int | None = None
) -> ScopedRepository:
    """The repository a token reads through: its grant's farm and scopes, actor `token:<id>`.

    Checked afresh on every call (never cached), so revoking or expiring the grant, or the issuer
    losing ownership of the farm or being disabled, refuses the next call. `farm_id`, when the
    caller names a farm, must be the token's. Records `last_used_at`. `TokenRejected` otherwise.
    """
    if (
        not isinstance(raw, str)
        or len(raw) > TOKEN_MAX_LEN
        or not raw.startswith(tuple(TOKEN_PREFIXES.values()))
    ):
        raise TokenRejected
    digest = token_hash(raw)
    token = session.scalar(select(AccessToken).where(AccessToken.token_hash == digest))
    if token is None or not hmac.compare_digest(token.token_hash, digest):
        raise TokenRejected
    # populate_existing: a long-lived session (a stdio agent process) must see a revocation
    # committed elsewhere, not the grant it loaded on an earlier call.
    grant = session.get(DataGrant, token.grant_id, populate_existing=True)
    now = datetime.now(UTC)
    if (
        grant is None
        or (farm_id is not None and grant.farm_id != farm_id)
        or grant.revoked_at is not None
        or (grant.valid_from is not None and grant.valid_from > now)
        or (grant.valid_to is not None and grant.valid_to <= now)
        or not set(grant.scopes or ()) <= TOKEN_SCOPES
        or not is_farm_owner(session, token.created_by_identity_id, grant.farm_id)
    ):
        raise TokenRejected
    token.last_used_at = now
    session.flush()
    return ScopedRepository(
        session,
        grant.farm_id,
        grant,
        actor=f"token:{token.id}",
        purpose=grant.purpose,
        grant_view=True,
        private_member=True,
    )


def has_farm_membership(request: Request, repo: Repo) -> bool:
    """True for an active member (or installation operator); false for demo, tour, anonymous."""
    from farmcore.core.identity import principal_for
    from farmcore.core.models.identity import FarmMembership, LocalIdentity
    from farmcore.core.tour import is_tour
    from farmcore.web import is_admin

    if is_admin(request):
        return True
    if is_tour(request):
        return False
    principal = principal_for(request)
    if principal is None:
        return False
    return (
        repo.session.scalar(
            select(FarmMembership.id)
            .join(LocalIdentity)
            .where(
                FarmMembership.farm_id == repo.farm_id,
                FarmMembership.identity_id == principal.identity_id,
                FarmMembership.revoked_at.is_(None),
                LocalIdentity.disabled_at.is_(None),
            )
        )
        is not None
    )


def require_farm_member(request: Request, repo: Repo) -> None:
    """Private planning reads need a current farm membership, not demo/tour access.

    The installation operator keeps break-glass authority; an anonymous demo visitor never
    becomes a member and a tour never authorizes private data.
    """
    if not has_farm_membership(request, repo):
        raise HTTPException(403, "Farm member required.")


def require_farm_owner(request: Request, repo: Repo) -> None:
    """Farm writes require a current explicit owner membership, or installation operator.

    Tour mode removes the ability to write, not the owner's authority to read: an owner's
    tour still opens owner-only pages such as Sharing."""
    from farmcore.core.identity import principal_for
    from farmcore.core.models.identity import FarmMembership, LocalIdentity
    from farmcore.core.tour import is_tour
    from farmcore.web import is_admin

    if is_admin(request):
        return
    principal = principal_for(request)
    writing = request.method not in {"GET", "HEAD", "OPTIONS"}
    if principal is None or (writing and is_tour(request)):
        raise HTTPException(403, "Farm owner required.")
    # Serialize with membership edits, then re-read authority in the write transaction.
    if writing:
        repo.session.execute(select(Farm.id).where(Farm.id == repo.farm_id).with_for_update())
    active = repo.session.scalar(
        select(FarmMembership.id)
        .join(LocalIdentity)
        .where(
            FarmMembership.farm_id == repo.farm_id,
            FarmMembership.identity_id == principal.identity_id,
            FarmMembership.role == "owner",
            FarmMembership.revoked_at.is_(None),
            LocalIdentity.disabled_at.is_(None),
        )
    )
    if active is None:
        raise HTTPException(403, "Farm owner required.")
