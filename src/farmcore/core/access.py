"""Scoped data access: every route, job and service reads through a `ScopedRepository`.

A repository is bound to one farm, one grant, an actor and a purpose. Each read:

1. checks the grant (same farm, not revoked, inside `valid_from`..`valid_to`) and that it
   holds `scope` (the owner grant holds `*`), else `ScopeDenied`;
2. checks that `scope` covers the resource (`RESOURCE_SCOPES`: observations need
   `stations:read`, so a `logs:read` grant cannot read them by naming its own scope), else
   `ScopeDenied`; every farm table must be declared by core or by the owning app manifest —
   an undeclared resource is denied;
3. filters rows to the farm: directly (`farm_id`), via `log` (typed_quantity, log_source,
   log_parcel), via `asset` (observation), via `data_grant` (access_token) or via
   `weather_receipt` (weather observations and forecasts); a weather station is the farm's own
   or public (no owner); shared reference tables (source, catalog_item) are not
   farm-owned; any other table has no farm path and is read only through `execute`;
   a grant or agent-token view (`grant_view`) reads only weather values and receipts whose
   receipt allows redistribution (the licence gate); a caller-built statement labelled as, or
   selecting FROM, a weather value table is refused, and so are the legacy `observation` rows,
   which carry no licence. A grant view never learns whether withheld weather exists: no
   statement or predicate of its may name a weather value table at all, not even in an EXISTS,
   and `core/weather/attribution` words what it withheld from the grant's scope alone;
4. writes exactly one `access_log` row (actor, farm, grant, resource, scope, purpose).

`execute(stmt, scope, resource)` runs a caller-built statement as-is (the caller scopes it); it is
the only way to read farm-less tables such as `llm_audit` (`scope="audit:read"`).
Writes go through `repo.session`; with no per-user login, routes use the owner grant (`get_repo`).
Routes take `repo: Repo` (`Depends(get_repo, scope="function")`), so the request's writes and
access_log rows commit before the response is sent; a plain `Depends(get_repo)` fails at startup.
"""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.engine import Result
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.expression import TableClause
from sqlalchemy.sql.selectable import TextualSelect, Values

from farmcore.core import ext_ids
from farmcore.core.identity import token_hash
from farmcore.core.models.assessment import Assessment
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
    Source,
)
from farmcore.core.models.catalog import CatalogItem, LegacyQuantityMap, TypedQuantity
from farmcore.core.models.identity import (
    AccessToken,
    FarmMembership,
    LocalIdentity,
    OAuthToken,
)
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
        "weather:push",
    }
)
# An on-farm device's only scope (app weather_push): it stores readings and reads nothing, so it
# is in neither GRANT_READ_SCOPES nor TOKEN_SCOPES, and no resource lists it.
WEATHER_PUSH = "weather:push"
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
    TypedQuantity: (TypedQuantity.log_id, Log),
    LogSource: (LogSource.log_id, Log),
    LogParcel: (LogParcel.log_id, Log),
    Observation: (Observation.asset_id, Asset),
    AccessToken: (AccessToken.grant_id, DataGrant),
    OAuthToken: (OAuthToken.grant_id, DataGrant),
    WeatherObservation: (WeatherObservation.receipt_id, WeatherReceipt),
    WeatherForecast: (WeatherForecast.receipt_id, WeatherReceipt),
}
# Weather values a grant or token view reads only from redistributable receipts.
_LICENCE_GATED = frozenset({"weather_observation", "weather_forecast"})
# Rows a farm owns or every farm shares (null owner: a public network station).
_OWNED_OR_PUBLIC: dict[type[Base], Any] = {WeatherStation: WeatherStation.owner_farm_id}
# Reference data shared by every farm (product catalogue, data sources).
# Catalog items are public reference data (E-Phy imports) and records keep their own facts
# (a fertiliser's composition lives on the record). If farms ever write catalog items, drop
# CatalogItem from here and from RESOURCE_SCOPES' `_ANY`, and scope it to its farm.
_SHARED: frozenset[type[Base]] = frozenset({Source, CatalogItem})

_ANY = SCOPES - {WEATHER_PUSH}
_LOG_READS = frozenset({"logs:read", "logs:write", "register:read"})
# Which scopes may read which resource (table name). Every farm table must be declared here or
# by its app manifest (`Manifest.resource_scopes`); an undeclared resource is denied.
RESOURCE_SCOPES: dict[str, frozenset[str]] = {
    "access_log": frozenset({"audit:read"}),
    "access_token": frozenset({"audit:read"}),
    "app_module": frozenset({"audit:read"}),
    "asset": frozenset({"stations:read"}),
    "catalog_item": _ANY,  # shared reference (a fertiliser's or a spray product's name)
    # The assessment envelope (`core/assessments.py`): read by each loaded kind's own read
    # scope (`register_assessment_kind`), which reads that kind's rows only. No kind, no reader.
    "assessment": frozenset(),
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
    # OAuth connections' codes and hashed secrets (`core/oauth`): operational, never data.
    "oauth_code": frozenset({"audit:read"}),
    "oauth_token": frozenset({"audit:read"}),
    "local_identity": frozenset({"audit:read"}),
    "local_session": frozenset({"audit:read"}),
    "log": _LOG_READS,
    "log_parcel": _LOG_READS,
    "log_source": _LOG_READS,
    "login_bucket": frozenset({"audit:read"}),
    "observation": frozenset({"stations:read"}),
    "operation_correction": frozenset({"corrections:read"}),
    # Operational: deliveries outside the installation and their receipts (core/outbox.py).
    "outbox": frozenset({"audit:read"}),
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
    # core_0018's frozen maps: legacy ids kept in historic JSON and published ids resolve here.
    "legacy_product_map": _ANY,
    "legacy_quantity_map": _LOG_READS,
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
# Assessment kind key -> its read scope, as loaded apps declare them (`AssessmentKind.read_scope`).
KIND_READ_SCOPES: dict[str, str] = {}


def register_assessment_kind(key: str, read_scope: str) -> None:
    """Let `read_scope` read the envelopes of kind `key`, and only those (registered at
    discovery). A scope core does not know becomes known for the owner's reads only: it is never
    in `GRANT_READ_SCOPES` or `TOKEN_SCOPES`, so no grant preset, agent token or OAuth connection
    holds it by default. Idempotent; a kind or scope already registered differently is refused,
    so one kind's scope never reads another kind."""
    known = KIND_READ_SCOPES.get(key)
    if known == read_scope:
        return
    if known is not None:
        raise ValueError(f"assessment kind {key!r} is already read with {known!r}")
    for other, scope in KIND_READ_SCOPES.items():
        if scope == read_scope:
            raise ValueError(f"assessment kinds {other!r} and {key!r} share {read_scope!r}")
    KIND_READ_SCOPES[key] = read_scope
    RESOURCE_SCOPES["assessment"] = RESOURCE_SCOPES["assessment"] | {read_scope}


def _known(scope: str) -> bool:
    return scope in SCOPES or scope in KIND_READ_SCOPES.values()


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
                    # Spray products (core_0018): the item, its label facts and its crop
                    # protection attributes, and the frozen map old published ids resolve by.
                    "catalog_item",
                    "crop_protection_item",
                    "advisor_product",
                    "legacy_product_map",
                    "source",
                    "asset",
                    "weather_station",
                    "weather_receipt",
                    "weather_observation",
                    "weather_forecast",
                    "log",
                    # A spray's doses; readers keep to spray logs (another record's amounts are
                    # not doses).
                    "typed_quantity",
                    "legacy_quantity_map",
                    "log_parcel",
                    "log_source",
                    "ext_id",
                    "advice",
                }
            )
        ):
            raise ScopeDenied("This resource is not available through ordinary grants.")
        if not _known(scope):
            known = sorted(SCOPES | set(KIND_READ_SCOPES.values()))
            raise ValueError(f"unknown scope {scope!r}; known: {known}")
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
        return self._list(model, where, (), scope, order_by, limit)

    def held_observations(
        self,
        *where: Any,
        as_of: datetime | None = None,
        scope: str = "stations:read",
        order_by: Any = None,
        limit: int | None = None,
    ) -> list[WeatherObservation]:
        """`weather.store.held`: the farm's observations matching `where` from the receipts
        this view reads (a grant view: redistributable only) fetched by `as_of` (None: all),
        a provisional value left out where one of those receipts holds its final value. So a
        withheld final never hides a shared provisional value, and a final fetched after
        `as_of` never hides the provisional value known then. `as_of` is a timezone-aware
        datetime: these filters are the repository's own and go unchecked, so a SQL expression
        there could read around the licence gate."""
        if as_of is not None and not (isinstance(as_of, datetime) and as_of.tzinfo is not None):
            raise ScopeDenied("held_observations() takes a timezone-aware datetime as_of.")
        known = select(WeatherReceipt.id).where(WeatherReceipt.farm_id == self.farm_id)
        if self.grant_view:
            known = known.where(WeatherReceipt.redistribution)
        if as_of is not None:
            known = known.where(WeatherReceipt.fetched_at <= as_of)
        own = (WeatherObservation.receipt_id.in_(known), ~_superseded(known))
        return self._list(WeatherObservation, where, own, scope, order_by, limit)

    def _list[M: Base](
        self,
        model: type[M],
        where: tuple[Any, ...],
        own: tuple[Any, ...],
        scope: str,
        order_by: Any,
        limit: int | None,
    ) -> list[M]:
        """`list`, with `own`: the repository's filters, which `_refuse_predicates` (the
        caller's arguments only) does not check."""
        stmt = self._scoped(model, scope).where(*own, *where)
        caller = select(model).where(*where)
        if order_by is not None:
            order = order_by if isinstance(order_by, (list, tuple)) else (order_by,)
            stmt = stmt.order_by(*order)
            caller = caller.order_by(*order)
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ScopeDenied("list() takes a plain int limit.")
            stmt = stmt.limit(limit)
        self._refuse_predicates(caller, model)
        self._authorise(scope, model.__table__.name)
        return list(self.session.scalars(stmt))

    def get[M: Base](self, model: type[M], id: Any, *, scope: str) -> M | None:
        pk = model.__mapper__.primary_key
        if len(pk) != 1:
            raise TypeError(f"{model.__name__} has a composite key; use list()")
        stmt = self._scoped(model, scope).where(pk[0] == id)
        self._refuse_predicates(select(model).where(pk[0] == id), model)
        self._authorise(scope, model.__table__.name)
        return self.session.scalar(stmt)

    def execute(self, stmt: Any, *, scope: str, resource: str) -> Result[Any]:
        """Run `stmt` unchanged (the caller filters it) after the scope check; logs `resource`."""
        _refuse_opaque(stmt)
        self._refuse_ungated(stmt, resource)
        self._refuse_envelope(stmt, resource)
        self._authorise(scope, resource)
        return self.session.execute(stmt)

    def execute_unaudited(self, stmt: Any, *, scope: str, resource: str) -> Result[Any]:
        """`execute` without the access_log row: the scope and grant are still checked. Only for
        decorative reads of data the same viewer already reads through an audited path (the
        parcel silhouettes: the map's layer already logs the geometry read)."""
        _refuse_opaque(stmt)
        self._refuse_ungated(stmt, resource)
        self._refuse_envelope(stmt, resource)
        self.check(scope, resource)
        return self.session.execute(stmt)

    @staticmethod
    def _refuse_envelope(stmt: Any, resource: str) -> None:
        """Assessment envelopes are read only through `list`/`get`, which keep a scope to its
        own kind; a caller-built statement on the table, or naming it anywhere (a join, a
        subquery, an EXISTS, a union), could not."""
        if resource == "assessment" or "assessment" in referenced_tables(stmt):
            raise ScopeDenied("Assessments are read through list() or get(), one kind per scope.")

    def _refuse_ungated(self, stmt: Any, resource: str) -> None:
        """A caller-built statement cannot be held to the licence gate: refused for grant views
        when labelled as a weather value table or naming one anywhere (a join, a subquery, an
        EXISTS, a union). Withheld weather is indistinguishable from none: not even a yes/no."""
        if not self.grant_view:
            return
        if resource in _LICENCE_GATED or referenced_tables(stmt) & _LICENCE_GATED:
            raise ScopeDenied("Weather values are shared only through the licence gate.")

    def _refuse_predicates(self, stmt: Select[Any], model: type[Base]) -> None:
        """`list`/`get` read one table: a caller's argument naming another table, an alias, a
        CTE or a subquery column would join it in implicitly, unscoped. Their WHERE and ORDER BY
        are held to `execute`'s rules: no opaque SQL, no assessment table (another kind's
        envelopes, read in a subquery or an EXISTS), and for grant views no weather value table
        outside the licence gate. `stmt` holds the caller's arguments only: the repository's own
        filters are not checked (`held_observations`' rule for superseded values reads
        weather_observation, from the receipts the view may read)."""
        froms = stmt.get_final_froms()
        if not (
            len(froms) == 1
            and isinstance(froms[0], TableClause)
            and froms[0].name == model.__table__.name
        ):
            raise ScopeDenied(
                f"list() and get() read {model.__table__.name} alone; join in execute()."
            )
        own = model.__table__.name
        parts = [p for p in (stmt.whereclause, stmt._order_by_clause) if p is not None]
        for part in parts:
            _refuse_opaque(part)
            tables = referenced_tables(part, own=own)
            if "assessment" in tables:
                raise ScopeDenied(
                    "Assessments are read through list() or get(), one kind per scope."
                )
            if self.grant_view and tables & _LICENCE_GATED:
                raise ScopeDenied("Weather values are shared only through the licence gate.")

    def _scoped[M: Base](self, model: type[M], scope: str) -> Select[tuple[M]]:
        stmt = select(model)
        if model is Assessment:
            # A scope reads only the kinds that declared it (`register_assessment_kind`); the
            # owner's `*` grant reads each kind through that kind's own scope too.
            kinds = [key for key, read in KIND_READ_SCOPES.items() if read == scope]
            stmt = stmt.where(Assessment.kind.in_(kinds))
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
        if model is WeatherReceipt and self.grant_view:
            return stmt.where(WeatherReceipt.farm_id == self.farm_id, WeatherReceipt.redistribution)
        if "farm_id" in model.__table__.c:
            return stmt.where(model.__table__.c.farm_id == self.farm_id)
        if model in _VIA_PARENT:
            fk, parent = _VIA_PARENT[model]
            owned = select(parent.id).where(parent.farm_id == self.farm_id)
            if self.grant_view and parent is WeatherReceipt:
                owned = owned.where(WeatherReceipt.redistribution)
            return stmt.where(fk.in_(owned))
        if model is LegacyQuantityMap:  # two hops: its typed quantity's log
            owned = (
                select(TypedQuantity.id)
                .join(Log, Log.id == TypedQuantity.log_id)
                .where(Log.farm_id == self.farm_id)
            )
            return stmt.where(LegacyQuantityMap.typed_quantity_id.in_(owned))
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


def _superseded(receipts: Select[Any]) -> Any:
    """A provisional observation whose final value (same station, variable and period) one of
    `receipts` holds (`ScopedRepository.held_observations`)."""
    final = aliased(WeatherObservation)
    return and_(
        WeatherObservation.provisional,
        exists().where(
            final.station_id == WeatherObservation.station_id,
            final.variable == WeatherObservation.variable,
            final.period_start == WeatherObservation.period_start,
            final.period_end == WeatherObservation.period_end,
            final.provisional.is_(False),
            final.receipt_id.in_(receipts),
        ),
    )


def referenced_tables(stmt: Any, own: str | None = None) -> set[str]:
    """Every table `stmt` names: the FROMs of it and of every statement nested in it (ORM joins
    included), and the table behind every column (an alias, a CTE, a subquery). A column of
    `own` (the table `list`/`get` read) is not a reference; a statement reading it is."""
    return {e.name for e in _elements(stmt, own) if isinstance(e, TableClause)}


def _elements(stmt: Any, own: str | None = None) -> Iterator[Any]:
    """Every node of `stmt`, with what `get_children()` skips: each nested statement's final
    FROMs (ORM joins, a join's ON tables) and each column's table."""
    seen: dict[int, Any] = {}  # keeps each node alive, so an id is never reused
    stack = [stmt]
    while stack:
        element = stack.pop()
        if id(element) in seen:
            continue
        seen[id(element)] = element
        yield element
        stack.extend(element.get_children())
        if hasattr(element, "get_final_froms"):
            stack.extend(element.get_final_froms())
        table = getattr(element, "table", None) if isinstance(element, ColumnClause) else None
        if table is not None and not (isinstance(table, TableClause) and table.name == own):
            stack.append(table)


_HARMLESS_LITERAL = re.compile(r"\*|\d+")  # count(*), EXISTS(SELECT *), select(1)


_ATTACHED_TEXT = ("_prefixes", "_suffixes", "_statement_hints", "_hints")


def _refuse_opaque(stmt: Any) -> None:
    """Text, a literal column, a VALUES payload or a prefix, suffix or hint (anywhere in
    `stmt`) can name tables where no check reads them: refused. `*` and a bare integer are
    the literal columns allowed."""
    for element in _elements(stmt):
        if (
            isinstance(element, (TextClause, TextualSelect, Values))
            or (
                isinstance(element, ColumnClause)
                and element.is_literal
                and not _HARMLESS_LITERAL.fullmatch(element.name)
            )
            or any(getattr(element, attr, None) for attr in _ATTACHED_TEXT)
        ):
            raise ScopeDenied(
                "Read through SQL expressions, not text, VALUES or hints, so their tables are "
                "checked."
            )


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
TOKEN_PREFIXES = {"personal": "fc_pat_", "oauth": "fc_oat_"}
# An OAuth refresh token: only the token endpoint takes it (`core/oauth`), never a resource.
REFRESH_PREFIX = "fc_ort_"
TOKEN_BYTES = 32
TOKEN_MAX_LEN = 128  # longer input is refused before hashing
# How stale Sharing's "Last used" may be: an HTTP agent authenticates on every request, and
# writing the token row each time would serialise its calls on that row.
LAST_USED_EVERY = timedelta(minutes=1)


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


def _oauth_connection(session: Session, digest: str, audience: str | None) -> AccessToken | None:
    """The OAuth connection a live access token of `digest` belongs to, if it was issued for
    `audience` (RFC 8707: a token for another resource, or presented where no resource is
    named, as on stdio, is refused)."""
    secret = session.scalar(
        select(OAuthToken).where(OAuthToken.token_hash == digest, OAuthToken.kind == "access")
    )
    if (
        secret is None
        or not hmac.compare_digest(secret.token_hash, digest)
        or secret.revoked_at is not None
        or secret.expires_at <= datetime.now(UTC)
    ):
        return None
    token = session.scalar(
        select(AccessToken).where(
            AccessToken.grant_id == secret.grant_id, AccessToken.kind == "oauth"
        )
    )
    if token is None or audience is None or token.audience != audience:
        return None
    return token


def authenticate_token(
    session: Session, raw: str, *, farm_id: int | None = None, audience: str | None = None
) -> ScopedRepository:
    """The repository a token reads through: its grant's farm and scopes, actor `token:<id>`.

    A personal token (`fc_pat_`) or an OAuth access token (`fc_oat_`); the latter only where
    the caller names the resource it serves (`audience`, the MCP endpoint's public URL) and it
    is that token's. Checked afresh on every call (never cached), so revoking or expiring the
    grant or the token, or the issuer losing ownership of the farm or being disabled, refuses
    the next call. `farm_id`, when the caller names a farm, must be the token's. Records
    `last_used_at` (at most once per `LAST_USED_EVERY`). `TokenRejected` otherwise.
    """
    if (
        not isinstance(raw, str)
        or len(raw) > TOKEN_MAX_LEN
        or not raw.startswith(tuple(TOKEN_PREFIXES.values()))
    ):
        raise TokenRejected
    digest = token_hash(raw)
    if raw.startswith(TOKEN_PREFIXES["oauth"]):
        token = _oauth_connection(session, digest, audience)
    else:
        token = session.scalar(select(AccessToken).where(AccessToken.token_hash == digest))
        if token is not None and not hmac.compare_digest(token.token_hash or "", digest):
            token = None
    if token is None:
        raise TokenRejected
    # populate_existing: a long-lived session (a stdio agent process) must see a revocation
    # committed elsewhere, not the grant it loaded on an earlier call.
    grant = session.get(DataGrant, token.grant_id, populate_existing=True)
    if grant is None or not grant_usable(session, grant, token.created_by_identity_id):
        raise TokenRejected
    if farm_id is not None and grant.farm_id != farm_id:
        raise TokenRejected
    now = datetime.now(UTC)
    if token.last_used_at is None or now - token.last_used_at >= LAST_USED_EVERY:
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


def grant_usable(session: Session, grant: DataGrant, issuer_id: int) -> bool:
    """Whether an agent's grant may still be used: not revoked, inside its validity, within
    what an agent token may hold, and its issuer still an owner of the farm."""
    now = datetime.now(UTC)
    return (
        grant.revoked_at is None
        and (grant.valid_from is None or grant.valid_from <= now)
        and (grant.valid_to is None or grant.valid_to > now)
        and set(grant.scopes or ()) <= TOKEN_SCOPES
        and is_farm_owner(session, issuer_id, grant.farm_id)
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
