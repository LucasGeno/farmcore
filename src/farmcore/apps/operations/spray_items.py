"""The one read seam over spray products and their doses.

Every reader of a spray's products and doses (register, label checks, stock, tools, map, NGSI,
chat, planning, ISOXML matching) reads through here, never `product` or `quantity` directly, so
moving the spray catalogue onto `catalog_item` and its crop-protection extension changes this
module and the writers only (the contract step). Today it reads `product` and `quantity`.

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
from sqlalchemy.orm import Session

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Farm, Product, Quantity
from farmcore.core.models.catalog import legacy_typed_quantity

# Farm prop holding the shortlist; `[]` offers nothing, absent offers every item.
SHORTLIST_KEY = "product_ids"


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


def _item(product: Product) -> SprayItem:
    return SprayItem(
        id=product.id,
        name=product.name,
        authorisation_no=product.authorisation_no,
        active_substance=product.active_substance,
        props=dict(product.props or {}),
    )


def _dose(q: Quantity) -> SprayDose:
    return SprayDose(
        id=q.id,
        log_id=q.log_id,
        item_id=q.product_id,
        measure=q.measure,
        value=q.value,
        unit=q.unit,
        label=q.label,
    )


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
    filters = (Product.id.in_(ids),) if ids is not None else ()
    order_by = Product.id if by_id else Product.name
    return [_item(p) for p in repo.list(Product, *filters, scope=scope, order_by=order_by)]


def all_items(repo: ScopedRepository, *, scope: str) -> list[SprayItem]:
    """Every item, on or off a shortlist (receipts name items a record once used)."""
    return [_item(p) for p in repo.list(Product, scope=scope)]


def items_by_id(
    repo: ScopedRepository, ids: Collection[int | None], *, scope: str
) -> dict[int, SprayItem]:
    """The items these ids name, on or off the shortlist; no read when there is none."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {p.id: _item(p) for p in repo.list(Product, Product.id.in_(wanted), scope=scope)}


def item(repo: ScopedRepository, item_id: int | None, *, scope: str) -> SprayItem | None:
    if item_id is None:
        return None
    product = repo.get(Product, item_id, scope=scope)
    return _item(product) if product is not None else None


def known_item_ids(session: Session, ids: Collection[int]) -> set[int]:
    """Which of these ids name an item at all (a draft's validator runs on a bare session)."""
    if not ids:
        return set()
    return set(session.scalars(select(Product.id).where(Product.id.in_(list(ids)))))


def doses_for_logs(
    repo: ScopedRepository, log_ids: Collection[int], *, scope: str, rates_only: bool = False
) -> dict[int, list[SprayDose]]:
    """Each log's doses in entry order (an absent log has none)."""
    out: dict[int, list[SprayDose]] = defaultdict(list)
    if not log_ids:
        return out
    where = [Quantity.log_id.in_(list(log_ids))]
    if rates_only:
        where.append(Quantity.measure == "rate")
    for q in repo.list(Quantity, *where, scope=scope, order_by=Quantity.id):
        out[q.log_id].append(_dose(q))
    return out


@dataclass(frozen=True, slots=True)
class StandardDose:
    """A dose in its standard measure, value and unit (null when the entered unit has none or
    the value is NaN), beside what was entered: what stock converts."""

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
    """The doses of these items on these (already scope-read) logs, in the standard shape from
    the frozen unit table."""
    if not log_ids:
        return []
    view = legacy_typed_quantity.c
    rows = repo.session.execute(
        select(
            view.log_id,
            view.product_id,
            view.measure,
            view.value,
            view.unit,
            view.entered_value,
            view.entered_unit,
        ).where(view.log_id.in_(list(log_ids)), view.product_id.in_(list(item_ids)))
    )
    return [StandardDose(*row) for row in rows]


def doses_of_log(log: Any) -> list[SprayDose]:
    """A loaded log's doses (its `quantities`), for readers that already hold the log."""
    return [_dose(q) for q in log.quantities]


def named_rates_of_log(log: Any) -> list[tuple[SprayDose, SprayItem | None]]:
    """A loaded log's rate doses, each with the item it names (read through the log's session,
    for readers that hold a log read under their scope but no repository). Only rates resolve
    their item, as the readers only name rates."""
    return [
        (_dose(q), _item(q.product) if q.product else None)
        for q in log.quantities
        if q.measure == "rate"
    ]
