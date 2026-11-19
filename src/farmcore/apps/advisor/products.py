"""Crop protection's view of a spray item: its disease-model attributes.

Contact or penetrant, and the diseases it targets (downy, powdery), belong to crop protection,
not to the record: operations' seam (`farmcore.apps.operations.spray_items`) carries the legal
record facts only, and every reader of these attributes asks here, by item id. They live in crop
protection's own `advisor_product` table (`farmcore.apps.advisor.catalog`).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from farmcore.apps.advisor.catalog import AdvisorProduct
from farmcore.apps.advisor.engine.types import ProductRef
from farmcore.core.access import ScopedRepository
from farmcore.core.models.catalog import CatalogItem

RESOURCE = "advisor_product"


def product_refs(
    repo: ScopedRepository, item_ids: Collection[int | None], *, scope: str
) -> dict[int, ProductRef]:
    """item id -> its crop-protection reference; an item crop protection does not know is
    absent (it gives no cover)."""
    wanted = {i for i in item_ids if i is not None}
    if not wanted:
        return {}
    rows = repo.execute(
        select(
            AdvisorProduct.catalog_item_id,
            CatalogItem.name,
            AdvisorProduct.product_type,
            AdvisorProduct.targets,
        )
        .join(CatalogItem, CatalogItem.id == AdvisorProduct.catalog_item_id)
        .where(AdvisorProduct.catalog_item_id.in_(wanted)),
        scope=scope,
        resource=RESOURCE,
    )
    return {
        row.catalog_item_id: ProductRef(
            name=row.name, product_type=row.product_type, targets=list(row.targets)
        )
        for row in rows
    }


def targets(repo: ScopedRepository, item_id: int | None, *, scope: str) -> tuple[str, ...]:
    """The diseases one item targets; none for an unknown item."""
    ref = product_refs(repo, [item_id], scope=scope).get(item_id) if item_id is not None else None
    return tuple(ref.targets) if ref else ()


def ensure_ref(
    session: Session, item_id: int, *, product_type: str, targets: Sequence[str]
) -> None:
    """Set one plant-protection item's attributes (the demo shortlist's seed writes them)."""
    values = {"product_type": product_type, "targets": list(targets)}
    session.execute(
        pg_insert(AdvisorProduct)
        .values(catalog_item_id=item_id, **values)
        .on_conflict_do_update(index_elements=["catalog_item_id"], set_=values)
    )
