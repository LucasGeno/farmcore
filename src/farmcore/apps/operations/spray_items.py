"""The one read seam over spray products and their doses.

Every reader of a spray's products and doses (register, label checks, stock, tools, map, NGSI,
chat, planning, ISOXML matching) reads through here. A product is a `catalog_item` with
operations' `crop_protection_item` extension; a dose is a `typed_quantity` on a spray log, kept
as entered (`entered_measure`, `entered_value`, `entered_unit`). Since core_0018 they are the
only copy: the legacy `product` and `quantity` tables are frozen for rollback, and
`legacy_product_map`/`legacy_quantity_map` resolve the ids historic JSON still holds.

- `SprayItem`: a product's legal record facts (Reg. (EU) 2023/564): name, authorisation number
  as printed (no country), active substance, and its props (placeholder flag, label limits,
  organic sources). The disease-model attributes (contact/penetrant, downy/powdery) are crop
  protection's, read through `farmcore.apps.advisor.products`, never here.
- `SprayDose`: a dose as entered on a record: measure, value and unit as entered, label, and the
  item it names (or none: a labelled product nobody matched).

Ids are opaque: an item id is only ever compared with another item id read here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from farmcore.apps.operations.models import CropProtectionItem
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Farm, Log
from farmcore.core.models.catalog import CatalogItem, LegacyProductMap, TypedQuantity

# Farm prop holding the shortlist (catalog item ids); `[]` offers nothing, absent offers every
# item. core_0018 renamed it from `product_ids`, so a reader of the old id space finds nothing.
SHORTLIST_KEY = "catalog_item_ids"
RESOURCE = "crop_protection_item"
SPRAY = "spray"


@dataclass(frozen=True, slots=True)
class SprayItem:
    id: int
    name: str
    authorisation_no: str
    active_substance: str
    props: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def placeholder(self) -> bool:
        return bool(self.props.get("placeholder"))


@dataclass(frozen=True, slots=True)
class SprayDose:
    id: int
    log_id: int
    item_id: int | None
    measure: str
    value: Decimal
    unit: str
    label: str | None

    @property
    def rate(self) -> bool:
        return self.measure == "rate"


_ITEMS = select(
    CatalogItem.id,
    CatalogItem.name,
    CropProtectionItem.authorisation_no,
    CropProtectionItem.active_substance,
    CropProtectionItem.props,
).join(CropProtectionItem, CropProtectionItem.catalog_item_id == CatalogItem.id)


def _item(row: Any) -> SprayItem:
    return SprayItem(
        id=row.id,
        name=row.name,
        authorisation_no=row.authorisation_no,
        active_substance=row.active_substance,
        props=dict(row.props or {}),
    )


def _dose(q: TypedQuantity) -> SprayDose:
    return SprayDose(
        id=q.id,
        log_id=q.log_id,
        item_id=q.catalog_item_id,
        measure=q.entered_measure,
        value=q.entered_value,
        unit=q.entered_unit,
        label=q.label,
    )


def _items(repo: ScopedRepository, *where: Any, scope: str, order_by: Any) -> list[SprayItem]:
    stmt = _ITEMS.where(*where).order_by(order_by)
    return [_item(row) for row in repo.execute(stmt, scope=scope, resource=RESOURCE)]


def offers_none(farm: Farm | None) -> bool:
    """The farm's shortlist is explicitly empty (no verified catalogue for its country)."""
    return farm is None or (farm.props or {}).get(SHORTLIST_KEY) == []


def items_for_farm(repo: ScopedRepository, *, scope: str, by_id: bool = False) -> list[SprayItem]:
    """The farm's shortlist, by name (or by id); an explicit empty list never inherits another
    country's products."""
    farm = repo.get(Farm, repo.farm_id, scope=scope)
    ids = (farm.props or {}).get(SHORTLIST_KEY) if farm else None
    if ids == []:
        return []
    filters = (CatalogItem.id.in_(ids),) if ids is not None else ()
    order_by = CatalogItem.id if by_id else CatalogItem.name
    return _items(repo, *filters, scope=scope, order_by=order_by)


def all_items(repo: ScopedRepository, *, scope: str) -> list[SprayItem]:
    """Every item, on or off a shortlist (receipts name items a record once used)."""
    return _items(repo, scope=scope, order_by=CatalogItem.id)


def items_by_id(
    repo: ScopedRepository, ids: Collection[int | None], *, scope: str
) -> dict[int, SprayItem]:
    """The items these ids name, on or off the shortlist; no read when there is none."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    found = _items(repo, CatalogItem.id.in_(wanted), scope=scope, order_by=CatalogItem.id)
    return {i.id: i for i in found}


def item(repo: ScopedRepository, item_id: int | None, *, scope: str) -> SprayItem | None:
    if item_id is None:
        return None
    return items_by_id(repo, [item_id], scope=scope).get(item_id)


def known_item_ids(session: Session, ids: Collection[int]) -> set[int]:
    """Which of these ids name an item at all (a draft's validator runs on a bare session)."""
    if not ids:
        return set()
    return set(
        session.scalars(
            select(CropProtectionItem.catalog_item_id).where(
                CropProtectionItem.catalog_item_id.in_(list(ids))
            )
        )
    )


def legacy_item_ids(session: Session, product_ids: Collection[int]) -> dict[int, int]:
    """Legacy product id -> the item it became (core_0018's frozen map); an id the map does not
    hold is absent, never guessed. For ids kept in historic JSON and old inputs."""
    if not product_ids:
        return {}
    rows = session.execute(
        select(LegacyProductMap.product_id, LegacyProductMap.catalog_item_id).where(
            LegacyProductMap.product_id.in_(list(product_ids))
        )
    )
    return dict(rows.tuples().all())


def doses_for_logs(
    repo: ScopedRepository, log_ids: Collection[int], *, scope: str, rates_only: bool = False
) -> dict[int, list[SprayDose]]:
    """Each spray log's doses in entry order (an absent log, or one of another record type, has
    none: another record's amounts are not doses)."""
    out: dict[int, list[SprayDose]] = defaultdict(list)
    if not log_ids:
        return out
    where = [
        TypedQuantity.log_id.in_(list(log_ids)),
        TypedQuantity.log.has(Log.type == SPRAY),
        TypedQuantity.entered_measure.is_not(None),
    ]
    if rates_only:
        where.append(TypedQuantity.entered_measure == "rate")
    for q in repo.list(TypedQuantity, *where, scope=scope, order_by=TypedQuantity.id):
        out[q.log_id].append(_dose(q))
    return out


@dataclass(frozen=True, slots=True)
class StandardDose:
    """A dose in its standard measure, value and unit (null when the entered unit has none),
    beside what was entered: what stock converts."""

    log_id: int
    item_id: int
    measure: str | None
    value: Decimal | None
    unit: str | None
    entered_value: Decimal
    entered_unit: str


def standard_doses(
    repo: ScopedRepository, log_ids: Collection[int], item_ids: Collection[int]
) -> list[StandardDose]:
    """The doses of these items on these (already scope-read) logs, in the standard shape."""
    if not log_ids:
        return []
    rows = repo.session.execute(
        select(
            TypedQuantity.log_id,
            TypedQuantity.catalog_item_id,
            TypedQuantity.measure,
            TypedQuantity.value,
            TypedQuantity.unit,
            TypedQuantity.entered_value,
            TypedQuantity.entered_unit,
        ).where(
            TypedQuantity.log_id.in_(list(log_ids)),
            TypedQuantity.catalog_item_id.in_(list(item_ids)),
            TypedQuantity.entered_measure.is_not(None),
        )
    )
    return [StandardDose(*row) for row in rows]


def doses_of_log(log: Log) -> list[SprayDose]:
    """A loaded spray log's doses, for readers that already hold the log."""
    return [_dose(q) for q in log.typed_quantities if q.entered_measure is not None]


def named_rates_of_log(log: Log) -> list[tuple[SprayDose, SprayItem | None]]:
    """A loaded log's rate doses, each with the item it names (read through the log's session,
    for readers that hold a log read under their scope but no repository). Only rates resolve
    their item, as the readers only name rates."""
    rates = [_dose(q) for q in log.typed_quantities if q.entered_measure == "rate"]
    ids = {d.item_id for d in rates if d.item_id is not None}
    session = object_session(log)
    found = (
        {r.id: _item(r) for r in session.execute(_ITEMS.where(CatalogItem.id.in_(ids)))}
        if ids and session is not None
        else {}
    )
    return [(d, found.get(d.item_id) if d.item_id is not None else None) for d in rates]
