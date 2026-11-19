"""Stock rows of the selected farm: purchased, used this season (computed), on hand, low stock.

`stock_rows(repo, now, tz)` reads this season's `inventory_stock` rows (season = local calendar
year of `now`) for products on the farm's shortlist (`operations.catalogue.products_for_farm`; an
explicit empty shortlist, as on the Spanish demo farm, shows no stock), then sums, per product,
the doses of the farm's *done* spray logs started between 1 Jan local and `now`.

Doses are read in the neutral shape: `legacy_typed_quantity` (spray product rates with their
standard measure, value and unit), keyed on the spray's product, never on a measure, so an
irrigation's entered volume (`typed_quantity`, no product) never consumes a liquid stock.
`core.units` converts them in `Decimal` with the log's area as the only factor:

- only rates count (`mass_rate`, `volume_rate`; every writer logs the dose as a per-hectare
  rate), so a total logged next to the rate is never added twice: the rate times the summed
  area of the log's parcels, in the stock unit;
- anything else (a litre dose against a kilogram stock, which needs a density nobody gave; a
  unit outside the registry; a NaN or infinite value; a log with no rate of the product) is
  counted in `not_computable`, and then neither `used` nor `on_hand` is given: a partial sum is
  never shown as the stock.

`on_hand = purchased − used`; `low` when `on_hand <= low_stock` (the farmer's own number).

`item_stock_rows(repo, now, tz)` does the same for rows of a neutral catalog item (a fertiliser):
used is the sum of the `typed_quantity` rows that name the item (`catalog_item_id`, never a
measure) on the farm's *done* records of any type this season, as entered, converted with the
factors the record's author entered (`log.props["factors"]`: a treated area, a density; else the
parcels' summed area and no density). A rate with no area, a volume against a mass stock with no
density, or an unknown unit makes that record not computable: no factor is ever assumed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Row, select

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core import units
from farmcore.core.access import ScopedRepository
from farmcore.core.logs import FACTORS
from farmcore.core.models.canonical import Log, LogParcel, Parcel, Product
from farmcore.core.models.catalog import CatalogItem, TypedQuantity, legacy_typed_quantity

LOGS_SCOPE = "logs:read"
FIELDS_SCOPE = "fields:read"
RATES = frozenset(m for m in units.STANDARD_UNITS if m.endswith("_rate"))


@dataclass(frozen=True)
class StockRow:
    stock: InventoryStock
    product: Product
    purchased: Decimal
    used: Decimal | None  # None: a spray of this product is in a unit that does not convert
    on_hand: Decimal | None
    sprays: int  # done sprays of this product counted this season
    not_computable: int  # of those, sprays whose dose does not convert into the stock unit
    low: bool

    @property
    def demo(self) -> bool:
        return bool((self.stock.props or {}).get("demo_data"))


def season_start(now: datetime, tz: str) -> tuple[int, datetime]:
    local = now.astimezone(ZoneInfo(tz))
    return local.year, local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def amount_used(dose: Row, area_ha: Decimal, stock_unit: str) -> Decimal | None:
    """The amount of a per-hectare dose over `area_ha` in `stock_unit`, or None when no known
    factor converts it exactly."""
    try:
        rate = units.Measured(
            dose.measure,
            units.decimal_value(dose.value),
            dose.unit,
            dose.entered_value,
            dose.entered_unit,
        )
        return units.convert(rate, stock_unit, area_ha=area_ha)
    except units.UnitError:
        return None


def stock_rows(repo: ScopedRepository, *, now: datetime, tz: str) -> list[StockRow]:
    season, start = season_start(now, tz)
    products = {p.id: p for p in products_for_farm(repo, scope=LOGS_SCOPE)}
    if not products:
        return []
    stocks = repo.list(
        InventoryStock,
        InventoryStock.season == season,
        InventoryStock.product_id.in_(products),
        scope=LOGS_SCOPE,
        order_by=InventoryStock.id,
    )
    if not stocks:
        return []
    logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start >= start,
        Log.ts_start <= now,
        scope=LOGS_SCOPE,
    )
    log_ids = [log.id for log in logs]
    view = legacy_typed_quantity.c
    doses = (
        repo.session.execute(
            select(
                view.log_id,
                view.product_id,
                view.measure,
                view.value,
                view.unit,
                view.entered_value,
                view.entered_unit,
            ).where(view.log_id.in_(log_ids), view.product_id.in_([s.product_id for s in stocks]))
        ).all()
        if log_ids
        else []
    )
    area = _log_areas(repo, {d.log_id for d in doses})
    by_product: dict[int, list[Row]] = defaultdict(list)
    for d in doses:
        by_product[d.product_id].append(d)

    rows = []
    for s in stocks:
        total, bad = Decimal(0), set()
        per_log: dict[int, list[Row]] = defaultdict(list)
        for d in by_product[s.product_id]:
            per_log[d.log_id].append(d)
        sprays = set(per_log)
        for log_id, ds in per_log.items():
            rates = [d for d in ds if d.measure in RATES]
            if not rates or any(d.measure is None for d in ds):
                bad.add(log_id)
            for d in rates:
                amount = amount_used(d, area.get(log_id, Decimal(0)), s.unit)
                if amount is None:
                    bad.add(log_id)
                else:
                    total += amount
        season_used = None if bad else total
        on_hand = None if season_used is None else s.purchased - season_used
        rows.append(
            StockRow(
                stock=s,
                product=products[s.product_id],
                purchased=s.purchased,
                used=season_used,
                on_hand=on_hand,
                sprays=len(sprays),
                not_computable=len(bad),
                low=on_hand is not None and s.low_stock is not None and on_hand <= s.low_stock,
            )
        )
    return rows


def _log_areas(repo: ScopedRepository, log_ids: set[int]) -> dict[int, Decimal]:
    """Summed parcel area (ha) per log."""
    if not log_ids:
        return {}
    links = repo.list(LogParcel, LogParcel.log_id.in_(log_ids), scope=LOGS_SCOPE)
    parcel_ids = {link.parcel_id for link in links}
    areas = {
        p.id: p.area_ha for p in repo.list(Parcel, Parcel.id.in_(parcel_ids), scope=FIELDS_SCOPE)
    }
    out: dict[int, Decimal] = defaultdict(Decimal)
    for link in links:
        out[link.log_id] += areas.get(link.parcel_id, Decimal(0))
    return dict(out)


@dataclass(frozen=True)
class ItemStockRow:
    stock: InventoryStock
    name: str
    purchased: Decimal
    used: Decimal | None  # None: a record of this item does not convert into the stock unit
    on_hand: Decimal | None
    records: int  # done records of this item counted this season
    not_computable: int
    low: bool


def _factor(props: dict, key: str) -> Decimal | None:
    raw = ((props or {}).get(FACTORS) or {}).get(key)
    return None if raw is None else Decimal(str(raw))


def item_used(typed: TypedQuantity, log: Log, parcel_area_ha: Decimal, stock_unit: str):
    """The amount of `typed` in `stock_unit`, or None when no entered factor converts it."""
    if typed.measure is None:
        return None
    area = _factor(log.props, "area_ha")
    if area is None and parcel_area_ha > 0:  # a parcel without a known area is no factor
        area = parcel_area_ha
    try:
        quantity = units.Measured(
            typed.measure,
            units.decimal_value(typed.value),
            typed.unit,
            typed.entered_value,
            typed.entered_unit,
        )
        return units.convert(
            quantity,
            stock_unit,
            area_ha=area,
            density_kg_per_l=_factor(log.props, "density_kg_per_l"),
        )
    except units.UnitError:
        return None


def item_stock_rows(repo: ScopedRepository, *, now: datetime, tz: str) -> list[ItemStockRow]:
    season, start = season_start(now, tz)
    stocks = repo.list(
        InventoryStock,
        InventoryStock.season == season,
        InventoryStock.catalog_item_id.is_not(None),
        scope=LOGS_SCOPE,
        order_by=InventoryStock.id,
    )
    if not stocks:
        return []
    item_ids = [s.catalog_item_id for s in stocks]
    names = dict(
        repo.execute(
            select(CatalogItem.id, CatalogItem.name).where(CatalogItem.id.in_(item_ids)),
            scope=LOGS_SCOPE,
            resource="catalog_item",
        ).all()
    )
    typed = repo.list(TypedQuantity, TypedQuantity.catalog_item_id.in_(item_ids), scope=LOGS_SCOPE)
    logs = {
        log.id: log
        for log in repo.list(
            Log,
            Log.id.in_({t.log_id for t in typed}),
            Log.status == "done",
            Log.ts_start >= start,
            Log.ts_start <= now,
            scope=LOGS_SCOPE,
        )
    }
    area = _log_areas(repo, set(logs))
    rows = []
    for s in stocks:
        total, counted, bad = Decimal(0), set(), set()
        for t in typed:
            if t.catalog_item_id != s.catalog_item_id or t.log_id not in logs:
                continue
            counted.add(t.log_id)
            amount = item_used(t, logs[t.log_id], area.get(t.log_id, Decimal(0)), s.unit)
            if amount is None:
                bad.add(t.log_id)
            else:
                total += amount
        used = None if bad else units.plain(total)
        on_hand = None if used is None else s.purchased - used
        rows.append(
            ItemStockRow(
                stock=s,
                name=names.get(s.catalog_item_id, ""),
                purchased=s.purchased,
                used=used,
                on_hand=on_hand,
                records=len(counted),
                not_computable=len(bad),
                low=on_hand is not None and s.low_stock is not None and on_hand <= s.low_stock,
            )
        )
    return rows
