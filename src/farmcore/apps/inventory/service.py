"""Stock rows of the selected farm: purchased, used this season (computed), on hand, low stock.

`stock_rows(repo, now, tz)` reads this season's `inventory_stock` rows (season = local calendar
year of `now`) for products on the farm's shortlist (`operations.catalogue.products_for_farm`; an
explicit empty shortlist, as on the Spanish demo farm, shows no stock), then sums, per product,
the quantities of the farm's *done* spray logs started between 1 Jan local and `now`:

- only `rate` quantities count (every writer logs the dose as a `rate`), so a total logged next
  to the rate is never added twice: a rate per hectare (`kg/ha`, `g/ha`, `L/ha`, `ml/ha`) times
  the summed area of the log's parcels, converted into the stock unit when the dimension matches
  (mass → kg, volume → l);
- anything else (a litre dose against a kilogram stock, a rate not per hectare, an unknown unit,
  a log with no rate of the product) is counted in `not_computable`, and then neither `used` nor
  `on_hand` is given: a partial sum is never shown as the stock.

`on_hand = purchased − used`; `low` when `on_hand <= low_stock` (the farmer's own number).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Log, LogParcel, Parcel, Product, Quantity

LOGS_SCOPE = "logs:read"
FIELDS_SCOPE = "fields:read"
PER_HA = "/ha"
RATE = "rate"
# unit -> (stock unit, factor into it)
UNITS: dict[str, tuple[str, Decimal]] = {
    "kg": ("kg", Decimal(1)),
    "g": ("kg", Decimal("0.001")),
    "l": ("l", Decimal(1)),
    "ml": ("l", Decimal("0.001")),
}


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


def convert(value: Decimal, unit: str, area_ha: Decimal, stock_unit: str) -> Decimal | None:
    """The amount of a per-hectare dose over `area_ha` in `stock_unit`, or None when it does not
    convert."""
    u = unit.strip().lower()
    if not u.endswith(PER_HA):
        return None
    base = UNITS.get(u.removesuffix(PER_HA))
    if base is None or base[0] != stock_unit:
        return None
    return value * base[1] * area_ha


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
    quantities = (
        repo.list(
            Quantity,
            Quantity.log_id.in_(log_ids),
            Quantity.product_id.in_([s.product_id for s in stocks]),
            scope=LOGS_SCOPE,
        )
        if log_ids
        else []
    )
    area = _log_areas(repo, {q.log_id for q in quantities})
    by_product: dict[int, list[Quantity]] = defaultdict(list)
    for q in quantities:
        by_product[q.product_id].append(q)

    rows = []
    for s in stocks:
        used, bad = Decimal(0), set()
        per_log: dict[int, list[Quantity]] = defaultdict(list)
        for q in by_product[s.product_id]:
            per_log[q.log_id].append(q)
        sprays = set(per_log)
        for log_id, qs in per_log.items():
            rates = [q for q in qs if q.measure == RATE]
            if not rates:
                bad.add(log_id)
            for q in rates:
                amount = convert(q.value, q.unit, area.get(log_id, Decimal(0)), s.unit)
                if amount is None:
                    bad.add(log_id)
                else:
                    used += amount
        total = None if bad else used
        on_hand = None if total is None else s.purchased - total
        rows.append(
            StockRow(
                stock=s,
                product=products[s.product_id],
                purchased=s.purchased,
                used=total,
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
