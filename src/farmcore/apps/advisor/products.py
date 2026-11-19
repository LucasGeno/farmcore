"""Crop protection's view of a spray item: its disease-model attributes.

Contact or penetrant, and the diseases it targets (downy, powdery), belong to crop protection,
not to the record: operations' seam (`farmcore.apps.operations.spray_items`) carries the legal
record facts only, and every reader of these attributes asks here, by item id. Today they are
read from `product`; ticket 14's contract step moves them to crop protection's own table.
"""

from __future__ import annotations

from collections.abc import Collection

from farmcore.apps.advisor.engine.types import ProductRef
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Product


def product_refs(
    repo: ScopedRepository, item_ids: Collection[int | None], *, scope: str
) -> dict[int, ProductRef]:
    """item id -> its crop-protection reference; an item crop protection does not know is
    absent (it gives no cover)."""
    wanted = {i for i in item_ids if i is not None}
    if not wanted:
        return {}
    return {
        p.id: ProductRef(name=p.name, product_type=p.product_type, targets=p.targets)
        for p in repo.list(Product, Product.id.in_(wanted), scope=scope)
    }


def targets(repo: ScopedRepository, item_id: int | None, *, scope: str) -> tuple[str, ...]:
    """The diseases one item targets; none for an unknown item."""
    ref = product_refs(repo, [item_id], scope=scope).get(item_id) if item_id is not None else None
    return tuple(ref.targets) if ref else ()
