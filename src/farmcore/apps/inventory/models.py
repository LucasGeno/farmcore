"""Inventory table (migration `inventory_0001`): one `inventory_stock` row per farm, product and
season (calendar year, as the register's season checks count it).

The row holds only what the farmer knows from the invoice: `purchased` (everything bought for the
season, in `unit`), the current `lot`, the `last_purchase_on` date and an optional `low_stock`
threshold (a plain number the farmer sets, not an agronomic value). What was used is never stored:
`service` computes it from the logged sprays, so a spray is never entered twice. `farm_id` lets the
scoped repository filter the table like any farm table. `props.demo_data` marks seeded rows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

STOCK_UNITS = ("kg", "l")


class InventoryStock(Base):
    __tablename__ = "inventory_stock"
    __table_args__ = (
        UniqueConstraint("farm_id", "product_id", "season"),
        CheckConstraint("unit IN ('kg', 'l')", name="unit"),
        CheckConstraint("purchased >= 0", name="purchased"),
        CheckConstraint("low_stock IS NULL OR low_stock >= 0", name="low_stock"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("product.id"))
    season: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(Text)
    purchased: Mapped[Decimal] = mapped_column(Numeric)
    lot: Mapped[str | None] = mapped_column(Text)
    last_purchase_on: Mapped[date | None] = mapped_column(Date)
    low_stock: Mapped[Decimal | None] = mapped_column(Numeric)
    props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
