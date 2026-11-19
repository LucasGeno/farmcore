"""Spray products for tests: a catalog item with operations' plant-protection extension and,
when given a type, crop protection's attributes (what `operations.seed.ensure_products` writes)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from farmcore.apps.advisor.catalog import AdvisorProduct
from farmcore.apps.operations.models import CropProtectionItem
from farmcore.core.models.catalog import CatalogItem


def add_product(
    session: Session,
    name: str,
    *,
    authorisation_no: str | None = None,
    active_substance: str | None = None,
    product_type: str | None = "contact",
    targets: Sequence[str] = (),
    props: Mapping[str, Any] | None = None,
) -> CatalogItem:
    item = CatalogItem(name=name)
    session.add(item)
    session.flush()
    session.add(
        CropProtectionItem(
            catalog_item_id=item.id,
            authorisation_no=authorisation_no or f"TEST-{name}",
            active_substance=active_substance or name,
            props=dict(props or {}),
        )
    )
    session.flush()
    if product_type is not None:
        session.add(
            AdvisorProduct(
                catalog_item_id=item.id, product_type=product_type, targets=list(targets)
            )
        )
        session.flush()
    return item


def entered(quantities) -> list[tuple[Any, ...]]:
    """(entered measure, value, unit, item id, label) of typed quantities, as entered."""
    return [
        (q.entered_measure, q.entered_value, q.entered_unit, q.catalog_item_id, q.label)
        for q in quantities
    ]
