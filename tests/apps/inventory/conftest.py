"""Inventory test fixtures: the default farm with 2 synthetic parcels, the product shortlist, and
helpers to log sprays and stock rows. Values are synthetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import clock, ext_ids, logs
from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import Farm, Parcel, Product
from farmcore.core.seed import ensure_default_farm

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
TEST_SOURCE = "test-inventory-parcels"


@dataclass
class InvFarm:
    farm: Farm
    parcels: list[Parcel]
    products: dict[str, Product]


def make_parcel(session, farm: Farm, n: int, area: str) -> Parcel:
    parcel, _ = ext_ids.upsert(
        session,
        source=TEST_SOURCE,
        key=f"{farm.id}-{n}",
        model=Parcel,
        values={
            "farm_id": farm.id,
            "name": f"Test parcel {n}",
            "alias": f"P{n}",
            "iacs_id": None,
            "area_ha": Decimal(area),
            "geom": SQUARE,
        },
    )
    return parcel


@pytest.fixture
def inv(session, settings, monkeypatch) -> InvFarm:
    monkeypatch.setattr(ops_seed, "get_settings", lambda: settings)
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    farm = ensure_default_farm(session, settings)
    parcels = [make_parcel(session, farm, 1, "1.25"), make_parcel(session, farm, 2, "0.75")]
    products = ops_seed.ensure_products(session)
    session.flush()
    return InvFarm(farm, parcels, products)


def spray(
    session,
    farm: Farm,
    parcels: list[Parcel],
    product: Product,
    *,
    at: datetime,
    value: str,
    unit: str,
    status: str = "done",
    key: str,
    measure: str = "rate",
    extra: tuple[logs.QuantityInput, ...] = (),
):
    log, _ = logs.record_operation(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[p.id for p in parcels],
        ts_start=at,
        ts_end=None,
        quantities=[
            logs.QuantityInput(
                measure=measure,
                value=Decimal(value),
                unit=unit,
                product_id=product.id,
                label=product.name,
            ),
            *extra,
        ],
        source=logs.SourceInput("file", key, {}, None, logs.RANK_FILE),
        actor="test",
        occurred_at=at,
        status=status,
    )
    return log


def stock(session, farm: Farm, product: Product, **values) -> InventoryStock:
    row = InventoryStock(
        farm_id=farm.id,
        product_id=product.id,
        season=values.pop("season", 2024),
        unit=values.pop("unit", "kg"),
        purchased=Decimal(values.pop("purchased", "20")),
        **values,
    )
    session.add(row)
    session.flush()
    return row


def at(session, settings, t: datetime) -> None:
    clock.set_replay(session, t, settings)
    session.commit()


T_APR = datetime(2024, 4, 20, 8, 0, tzinfo=UTC)
