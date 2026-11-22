"""Operation logs with provenance: write, merge, pending → confirm / cancel.

Record types keep their own policy (`POLICIES`). A spray carries product rates
(`QuantityInput`, read by stock, the register and the label checks) and merges by the spray rule
below. Every other record type never merges and takes only entered quantities
(`EnteredQuantity`): an irrigation can never carry a product rate. Both are stored as
`typed_quantity` rows; a spray's keep the measure they were entered as (`entered_measure`, `rate`
for a dose), another record's have none. A quantity of the other kind is refused before anything
is written.

Merge rule: an incoming *done spray* matches a done spray log on
the same farm whose parcel set overlaps and whose `ts_start` is within `MERGE_WINDOW`. A match
attaches a `log_source` (and the incoming parcels); if the incoming `precision_rank` beats the
best rank already on the log, its interval (`ts_start` and `ts_end`, even an empty end) and
quantities replace the log's (machine beats voice for dose and time). No match → a new log.
Re-sending a source already held on the same farm (same `source_type` + `external_key`) changes
nothing, so re-running an import is a no-op. Every write posts one event per parcel of the result;
every `log.*` event payload carries `source_types`, the types of the source(s) that write is about.

By default merges are unbounded by the replay clock. Callers can pass `not_after` to exclude
later merge targets without changing the incoming operation's event timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import blake2b
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from farmcore.core.models.canonical import Log, LogParcel, LogSource, Parcel
from farmcore.core.models.catalog import TypedQuantity, typed_quantity
from farmcore.core.timeline import post_event

# Source: the canonical data model merge rule.
MERGE_WINDOW = timedelta(hours=12)
# Precision ranks, higher wins for time and dose:
# machine (ISOXML) > farmer-confirmed planned work > file > voice > seed.
RANK_MACHINE = 30
RANK_PLANNING = 25
RANK_FILE = 20
RANK_VOICE = 10
RANK_SEED = 5
DEFAULT_SOURCE_APP = "core"


@dataclass(frozen=True)
class SourceInput:
    source_type: str  # log_source.source_type: voice, seed, isoxml, file
    external_key: str
    raw_payload: dict[str, Any]
    confidence: float | None
    precision_rank: int


@dataclass(frozen=True)
class QuantityInput:
    measure: str  # rate, volume, area
    value: Decimal
    unit: str
    catalog_item_id: int | None = None  # a plant-protection item (`crop_protection_item`)
    label: str | None = None


@dataclass(frozen=True)
class EnteredQuantity:
    """An amount on a record that is not a spray, kept exactly as entered (`20 mm`, `12.5 m3`);
    `core.units` adds the standard value when it knows the unit, else it stays null. With a
    `catalog_item_id` the amount is of that item (a fertiliser), and its stock counts it."""

    value: Decimal
    unit: str
    label: str | None = None
    catalog_item_id: int | None = None


# `log.props` key of the conversion factors a record's author entered for its quantities
# (`{"area_ha": "1.5", "density_kg_per_l": "1.32"}`, decimal strings): stock reads use them
# instead of the parcels' area, and never assume a density that is absent.
FACTORS = "factors"


@dataclass(frozen=True)
class RecordPolicy:
    merge_window: timedelta | None  # None: never merged with another record
    product_rates: bool  # quantities are product rates (stock, register, label checks)


POLICIES: dict[str, RecordPolicy] = {"spray": RecordPolicy(MERGE_WINDOW, product_rates=True)}
UNMERGED = RecordPolicy(None, product_rates=False)


def policy(type: str) -> RecordPolicy:
    return POLICIES.get(type, UNMERGED)


QuantityLike = QuantityInput | Mapping[str, Any]
RecordQuantity = QuantityLike | EnteredQuantity


def _split(
    type: str, items: Sequence[RecordQuantity]
) -> tuple[list[QuantityLike], list[EnteredQuantity]]:
    """The record's product rates and entered quantities; the kind its type does not take raises."""
    entered = [q for q in items if isinstance(q, EnteredQuantity)]
    rates = [q for q in items if not isinstance(q, EnteredQuantity)]
    if policy(type).product_rates and entered:
        raise ValueError(f"a {type} takes product-rate quantities, not entered quantities")
    if not policy(type).product_rates and rates:
        raise ValueError(f"a {type} takes entered quantities only, never product rates")
    return rates, entered


def _enter(session: Session, log: Log, entered: Sequence[EnteredQuantity]) -> None:
    log.typed_quantities.extend(
        typed_quantity(value=q.value, unit=q.unit, label=q.label, catalog_item_id=q.catalog_item_id)
        for q in entered
    )
    session.flush()


def _quantities(items: Sequence[QuantityLike]) -> list[TypedQuantity]:
    out = []
    for item in items:
        q = item if isinstance(item, QuantityInput) else QuantityInput(**item)
        out.append(
            typed_quantity(
                value=Decimal(str(q.value)),
                unit=q.unit,
                catalog_item_id=q.catalog_item_id,
                label=q.label,
                entered_measure=q.measure,
            )
        )
    return out


def _quantity_key(quantity: TypedQuantity) -> int | tuple[str | None, str | None, str]:
    if quantity.catalog_item_id is not None:
        return quantity.catalog_item_id
    return quantity.entered_measure, quantity.label, quantity.entered_unit


def _log_source(source: SourceInput) -> LogSource:
    return LogSource(
        source_type=source.source_type,
        external_key=source.external_key,
        raw_payload=dict(source.raw_payload),
        confidence=source.confidence,
        precision_rank=source.precision_rank,
    )


def lock_farm(session: Session, farm_id: int) -> None:
    """Serialize operation decisions until transaction end, before any operation row lock.

    Negative namespaced keys avoid the replay job's positive advisory-lock key. Hash
    collisions only serialize unrelated farms: source identity is checked separately in SQL.
    Autoflush is disabled until the lock is acquired. Callers must
    enter before flushing operation mutations or taking operation row locks themselves.
    """
    digest = blake2b(f"farmcore:operations:{farm_id}".encode(), digest_size=8).digest()
    key = -1 - (int.from_bytes(digest, "big") >> 1)
    with session.no_autoflush:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _parcels(session: Session, farm_id: int, parcel_ids: Sequence[int]) -> list[Parcel]:
    ids = list(dict.fromkeys(parcel_ids))
    if not ids:
        raise ValueError("an operation needs at least one parcel")
    found = list(
        session.scalars(select(Parcel).where(Parcel.id.in_(ids), Parcel.farm_id == farm_id))
    )
    missing = set(ids) - {p.id for p in found}
    if missing:
        raise ValueError(f"parcel ids {sorted(missing)} are not on farm {farm_id}")
    return sorted(found, key=lambda p: ids.index(p.id))


def _post(
    session: Session,
    log: Log,
    kind: str,
    verb: str,
    *,
    actor: str,
    occurred_at: datetime,
    source_app: str,
    source_types: Sequence[str],
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "log_id": log.id,
        "type": log.type,
        "status": log.status,
        "source_types": list(dict.fromkeys(source_types)),
        **(extra or {}),
    }
    # `log.parcels` loads without ORDER BY (heap order); emit per parcel in id order so the
    # timeline and activity list a log's parcels the same way every time.
    for parcel in sorted(log.parcels, key=lambda p: p.id):
        post_event(
            session,
            farm_id=log.farm_id,
            res_model="parcel",
            res_id=parcel.id,
            kind=kind,
            body=f"{log.type} {verb}",
            payload=payload,
            actor=actor,
            source_app=source_app,
            occurred_at=occurred_at,
        )


def _best_rank(log: Log) -> int:
    return max((s.precision_rank for s in log.sources), default=0)


def _holds(log: Log, source_type: str, external_key: str) -> bool:
    return any((s.source_type, s.external_key) == (source_type, external_key) for s in log.sources)


def _merge_target(
    session: Session,
    *,
    farm_id: int,
    type: str,
    parcel_ids: Sequence[int],
    ts_start: datetime,
    exclude_id: int | None = None,
    not_after: datetime | None = None,
) -> Log | None:
    """The closest done log of a mergeable type matching the rule, or None."""
    window = policy(type).merge_window
    if window is None:
        return None
    stmt = select(Log).where(
        Log.farm_id == farm_id,
        Log.type == type,
        Log.status == "done",
        Log.ts_start >= ts_start - window,
        Log.ts_start <= ts_start + window,
        Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id.in_(list(parcel_ids)))),
    )
    if not_after is not None:
        stmt = stmt.where(Log.ts_start <= not_after)
    if exclude_id is not None:
        stmt = stmt.where(Log.id != exclude_id)
    candidates = list(session.scalars(stmt.execution_options(populate_existing=True)))
    if not candidates:
        return None
    return min(candidates, key=lambda log: (abs(log.ts_start - ts_start), log.id))


def _new_log(
    session: Session,
    *,
    farm_id: int,
    type: str,
    status: str,
    parcels: list[Parcel],
    ts_start: datetime,
    ts_end: datetime | None,
    quantities: Sequence[RecordQuantity],
    source: SourceInput,
    name: str | None,
) -> Log:
    rates, entered = _split(type, quantities)
    log = Log(
        farm_id=farm_id,
        type=type,
        status=status,
        name=name or f"{type} {ts_start:%Y-%m-%d}",
        ts_start=ts_start,
        ts_end=ts_end,
    )
    log.parcels.extend(parcels)
    log.typed_quantities.extend(_quantities(rates))
    log.sources.append(_log_source(source))
    session.add(log)
    session.flush()
    _enter(session, log, entered)
    return log


def _add_parcels(log: Log, parcels: Sequence[Parcel]) -> None:
    have = {p.id for p in log.parcels}
    log.parcels.extend(p for p in parcels if p.id not in have)


def record_operation(
    session: Session,
    *,
    farm_id: int,
    type: str,
    parcel_ids: Sequence[int],
    ts_start: datetime,
    ts_end: datetime | None,
    quantities: Sequence[RecordQuantity],
    source: SourceInput,
    actor: str,
    occurred_at: datetime,
    status: str = "done",
    name: str | None = None,
    source_app: str = DEFAULT_SOURCE_APP,
    not_after: datetime | None = None,
) -> tuple[Log, bool]:
    """Write an operation, returning `(log, merged)`; a held source returns `(log, True)`.

    `not_after` optionally bounds merge targets. Source retries never change the held log,
    even outside the merge window. The farm lock lasts until caller commit or rollback.
    """
    _split(type, quantities)
    lock_farm(session, farm_id)
    parcels = _parcels(session, farm_id, parcel_ids)
    held = session.scalar(
        select(Log)
        .join(LogSource)
        .where(
            Log.farm_id == farm_id,
            LogSource.source_type == source.source_type,
            LogSource.external_key == source.external_key,
        )
        .order_by(Log.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if held is not None:
        return held, True
    target = None
    if status == "done":
        target = _merge_target(
            session,
            farm_id=farm_id,
            type=type,
            parcel_ids=[p.id for p in parcels],
            ts_start=ts_start,
            not_after=not_after,
        )
    if target is None:
        log = _new_log(
            session,
            farm_id=farm_id,
            type=type,
            status=status,
            parcels=parcels,
            ts_start=ts_start,
            ts_end=ts_end,
            quantities=quantities,
            source=source,
            name=name,
        )
        _post(
            session,
            log,
            "log.created",
            "logged",
            actor=actor,
            occurred_at=occurred_at,
            source_app=source_app,
            source_types=[source.source_type],
        )
        return log, False

    if _holds(target, source.source_type, source.external_key):
        return target, True  # the same source re-sent: nothing changes
    replaced = source.precision_rank > _best_rank(target)
    target.sources.append(_log_source(source))
    _add_parcels(target, parcels)
    if replaced:
        target.ts_start = ts_start
        target.ts_end = ts_end
        if quantities:
            target.typed_quantities = _quantities(quantities)
    session.flush()
    _post(
        session,
        target,
        "log.merged",
        "merged",
        actor=actor,
        occurred_at=occurred_at,
        source_app=source_app,
        source_types=[source.source_type],
        extra={"replaced": replaced},
    )
    return target, True


def create_pending_once(
    session: Session,
    *,
    farm_id: int,
    type: str,
    parcel_ids: Sequence[int],
    ts_start: datetime,
    ts_end: datetime | None,
    quantities: Sequence[RecordQuantity],
    source: SourceInput,
    actor: str,
    occurred_at: datetime,
    name: str | None = None,
    source_app: str = DEFAULT_SOURCE_APP,
) -> tuple[Log, bool]:
    """A pending log awaiting confirmation, and whether this call created it. Only a draft
    confirm (`operations.drafts`) creates one, and confirms or rolls it back in the same
    transaction: a draft is the one pre-confirm state.

    `True` means the source was already held (a replay or a concurrent duplicate): the existing
    draft is returned unchanged, and the caller must not write to it."""
    return record_operation(
        session,
        farm_id=farm_id,
        type=type,
        parcel_ids=parcel_ids,
        ts_start=ts_start,
        ts_end=ts_end,
        quantities=quantities,
        source=source,
        actor=actor,
        occurred_at=occurred_at,
        status="pending",
        name=name,
        source_app=source_app,
    )


def create_pending(
    session: Session,
    *,
    farm_id: int,
    type: str,
    parcel_ids: Sequence[int],
    ts_start: datetime,
    ts_end: datetime | None,
    quantities: Sequence[RecordQuantity],
    source: SourceInput,
    actor: str,
    occurred_at: datetime,
    name: str | None = None,
    source_app: str = DEFAULT_SOURCE_APP,
) -> Log:
    """A pending log awaiting confirmation (no merge check until `confirm`)."""
    log, _held = create_pending_once(
        session,
        farm_id=farm_id,
        type=type,
        parcel_ids=parcel_ids,
        ts_start=ts_start,
        ts_end=ts_end,
        quantities=quantities,
        source=source,
        actor=actor,
        occurred_at=occurred_at,
        name=name,
        source_app=source_app,
    )
    return log


def _pending_log(session: Session, log_id: int) -> Log:
    # Resolve the farm without taking a row lock (or autoflushing caller state). Every
    # mutation takes the advisory lock first, including confirmation and cancellation.
    with session.no_autoflush:
        farm_id = session.scalar(select(Log.farm_id).where(Log.id == log_id))
    if farm_id is None:
        raise LookupError(f"log {log_id} not found")
    lock_farm(session, farm_id)
    log = session.get(Log, log_id, with_for_update=True, populate_existing=True)
    if log is None:
        raise LookupError(f"log {log_id} not found")
    if log.status != "pending":
        raise ValueError(f"log {log_id} is {log.status!r}, not pending")
    return log


def confirm(
    session: Session,
    log_id: int,
    actor: str,
    occurred_at: datetime,
    *,
    source_app: str = DEFAULT_SOURCE_APP,
) -> tuple[Log, bool]:
    """Confirm a pending log: merge it into a matching done log (the pending row is deleted and
    its sources move), else mark it done. Returns `(log, merged)`."""
    pending = _pending_log(session, log_id)
    target = _merge_target(
        session,
        farm_id=pending.farm_id,
        type=pending.type,
        parcel_ids=[p.id for p in pending.parcels],
        ts_start=pending.ts_start,
        exclude_id=pending.id,
    )
    if target is None:
        pending.status = "done"
        session.flush()
        _post(
            session,
            pending,
            "log.confirmed",
            "confirmed",
            actor=actor,
            occurred_at=occurred_at,
            source_app=source_app,
            source_types=[s.source_type for s in pending.sources],
        )
        return pending, False

    replaced = _best_rank(pending) > _best_rank(target)
    if not replaced:
        have = {_quantity_key(q) for q in target.typed_quantities}
        new = [q for q in pending.typed_quantities if _quantity_key(q) not in have]
        pending_parcels = {p.id for p in pending.parcels}
        if new and any(p.id not in pending_parcels for p in target.parcels):
            # Quantities apply to every parcel of a log. Keep a new product on only
            # the parcels that reported it, without recording the old product twice.
            # Existing products still follow the duplicate rule on newly reported parcels.
            target_parcels = {p.id for p in target.parcels}
            parcels_added = [p.id for p in pending.parcels if p.id not in target_parcels]
            _add_parcels(target, pending.parcels)
            if parcels_added:
                session.flush()
                _post(
                    session,
                    target,
                    "log.merged",
                    "merged",
                    actor=actor,
                    occurred_at=occurred_at,
                    source_app=source_app,
                    source_types=[s.source_type for s in pending.sources],
                    extra={
                        "parcels_added": parcels_added,
                        "from_confirmed_log_id": log_id,
                    },
                )
            dropped_items = []
            for q in list(pending.typed_quantities):
                if _quantity_key(q) in have:
                    dropped_items.append(
                        q.catalog_item_id if q.catalog_item_id is not None else q.label
                    )
                    pending.typed_quantities.remove(q)
            pending.status = "done"
            session.flush()
            _post(
                session,
                pending,
                "log.confirmed",
                "confirmed",
                actor=actor,
                occurred_at=occurred_at,
                source_app=source_app,
                source_types=[s.source_type for s in pending.sources],
                extra={"duplicate_of": target.id, "dropped_items": dropped_items},
            )
            return pending, False
    added_items: list[int | str | None] = []
    _add_parcels(target, pending.parcels)
    if replaced:
        target.ts_start = pending.ts_start
        target.ts_end = pending.ts_end
        if pending.typed_quantities:
            target.typed_quantities.clear()
            session.flush()
            for q in list(pending.typed_quantities):
                q.log = target
    else:
        have = {_quantity_key(q) for q in target.typed_quantities}
        for q in list(pending.typed_quantities):
            key = _quantity_key(q)
            if key not in have:
                q.log = target
                have.add(key)
                added_items.append(q.catalog_item_id if q.catalog_item_id is not None else q.label)
    source_types = [s.source_type for s in pending.sources]
    for src in list(pending.sources):
        if not _holds(target, src.source_type, src.external_key):
            src.log = target  # a source the target already holds is deleted with the pending
    session.flush()
    session.delete(pending)
    session.flush()
    session.refresh(target)
    _post(
        session,
        target,
        "log.merged",
        "merged",
        actor=actor,
        occurred_at=occurred_at,
        source_app=source_app,
        source_types=source_types,
        extra={
            "replaced": replaced,
            "confirmed_log_id": log_id,
            "added_items": added_items,
        },
    )
    return target, True


def cancel(
    session: Session,
    log_id: int,
    actor: str,
    occurred_at: datetime,
    *,
    source_app: str = DEFAULT_SOURCE_APP,
) -> None:
    """Discard a pending log (its quantities and sources go with it)."""
    pending = _pending_log(session, log_id)
    _post(
        session,
        pending,
        "log.cancelled",
        "cancelled",
        actor=actor,
        occurred_at=occurred_at,
        source_app=source_app,
        source_types=[s.source_type for s in pending.sources],
    )
    session.delete(pending)
    session.flush()
