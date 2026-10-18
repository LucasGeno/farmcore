"""Inventory seed, scene B only: a few demo stock rows on the French demo farm (`props.demo_data`).

The quantities, lots and dates are made-up demo invoices (labelled "Demo data" on the page), not
agronomic values; usage is never seeded, it comes from the logged sprays. The Spanish demo farm
gets no stock: it has no verified product catalogue. Re-seeding leaves existing rows untouched
(one row per farm, product and season), so a farmer's low-stock threshold survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations.seed import ensure_products
from farmcore.core.seed import ensure_default_farm

SCENE_B = "b"
SEASON = 2024  # the replayed season


@dataclass(frozen=True)
class DemoStock:
    product: str  # operations.seed.SHORTLIST key
    purchased: str
    unit: str
    lot: str
    last_purchase_on: date
    low_stock: str | None


DEMO_STOCK: tuple[DemoStock, ...] = (
    DemoStock("copper-contact", "150", "kg", "DEMO-CU-2402", date(2024, 2, 20), "40"),
    DemoStock("sulphur-wettable", "75", "kg", "DEMO-S-2403", date(2024, 3, 12), "25"),
    DemoStock("cymoxanil-penetrant", "3", "kg", "DEMO-PJ-2403", date(2024, 3, 12), None),
)


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    if (scene or "").strip().lower() != SCENE_B:
        return {}
    farm = ensure_default_farm(session)
    products = ensure_products(session)
    held = set(
        session.scalars(
            select(InventoryStock.product_id).where(
                InventoryStock.farm_id == farm.id, InventoryStock.season == SEASON
            )
        )
    )
    for d in DEMO_STOCK:
        product = products[d.product]
        if product.id in held:
            continue
        session.add(
            InventoryStock(
                farm_id=farm.id,
                product_id=product.id,
                season=SEASON,
                unit=d.unit,
                purchased=Decimal(d.purchased),
                lot=d.lot,
                last_purchase_on=d.last_purchase_on,
                low_stock=None if d.low_stock is None else Decimal(d.low_stock),
                props={"demo_data": True},
            )
        )
    session.flush()
    return {"inventory_stock": len(DEMO_STOCK)}
