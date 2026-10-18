"""Preset-only season purchases for the demo farm's stock.

The 'Season 2024 complete' preset logs a whole season of sprays, which is more than the base demo
stock (`seed.DEMO_STOCK`) bought. `apply` tops each product's season row up with whole packs so the
stock ends above its low-stock number (or at or above 0 when none is set). There is no pack-size
data: a pack is the base seed's own purchase quantity of that product. A row holds one season's
purchase total, so the top-up is recorded on it (`props.season_purchases`: base, pack, packs added,
the label 'Demo data: season purchases') and found again through a stable ext id; applying again
recomputes from the recorded base and changes nothing. The base seed is not touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.inventory.seed import SEASON
from farmcore.apps.operations.seed import PRODUCT_SOURCE
from farmcore.core import ext_ids
from farmcore.core.access import DEMO_SOURCE
from farmcore.core.models.canonical import ExtId, Farm, Product

LABEL = "Demo data: season purchases"


def _key(farm_id: int, product_key: str) -> str:
    return f"season-2024-purchases:{farm_id}:{product_key}"


def apply(session: Session, farm: Farm, usage: Mapping[str, Decimal]) -> int:
    """Top up each product's season stock to cover `usage` (kg of product by shortlist key);
    returns the number of rows holding season purchases."""
    count = 0
    for product_key, used in usage.items():
        product = ext_ids.lookup(session, PRODUCT_SOURCE, product_key)
        if not isinstance(product, Product):
            continue
        row = session.scalar(
            select(InventoryStock).where(
                InventoryStock.farm_id == farm.id,
                InventoryStock.product_id == product.id,
                InventoryStock.season == SEASON,
            )
        )
        if row is None or row.unit != "kg":
            continue
        marker = (row.props or {}).get("season_purchases")
        base = Decimal(marker["base_purchased"]) if marker else row.purchased
        pack = Decimal(marker["pack"]) if marker else base
        needed = used + (row.low_stock if row.low_stock is not None else Decimal(0))
        packs = 0 if base > needed or pack <= 0 else int((needed - base) // pack) + 1
        row.purchased = base + pack * packs
        row.props = {
            **(row.props or {}),
            "demo_data": True,
            "season_purchases": {
                "label": LABEL,
                "base_purchased": str(base),
                "pack": str(pack),
                "packs_added": packs,
                "usage_kg": str(used),
            },
        }
        key = _key(farm.id, product_key)
        ext = session.scalar(select(ExtId).where(ExtId.source == DEMO_SOURCE, ExtId.key == key))
        if ext is None:
            session.flush()
            session.add(ExtId(source=DEMO_SOURCE, key=key, model="inventory_stock", res_id=row.id))
        else:
            ext.res_id = row.id
        count += 1
    session.flush()
    return count


def revert(session: Session, farm: Farm) -> int:
    """Demo reset hook: put each topped-up row back to its recorded base purchase and drop the
    note and ext id; returns the number of rows restored."""
    count = 0
    rows = session.scalars(
        select(InventoryStock).where(
            InventoryStock.farm_id == farm.id, InventoryStock.season == SEASON
        )
    )
    for row in rows:
        marker = (row.props or {}).get("season_purchases")
        if not marker:
            continue
        row.purchased = Decimal(marker["base_purchased"])
        row.props = {k: v for k, v in row.props.items() if k != "season_purchases"}
        session.execute(
            delete(ExtId).where(
                ExtId.source == DEMO_SOURCE,
                ExtId.model == "inventory_stock",
                ExtId.res_id == row.id,
                ExtId.key.startswith("season-2024-purchases:"),
            )
        )
        count += 1
    session.flush()
    return count
