"""Neutral catalog items and typed quantities, beside the legacy spray data (migration core_0006).

Expand step: `product` and `quantity` stay the only copy of legacy data and the source every
current consumer reads. Their neutral shape is two database views, so it cannot drift from them
whoever writes (ORM, Core, raw SQL, a restore):

- `legacy_catalog_item`: each product as a catalog item with its crop-protection attributes.
- `legacy_typed_quantity`: each quantity with its entered value and unit, and the standard
  measure/value/unit from the frozen `unit_definition` table. A unit outside that table, or a
  NaN value, has a null standard value: NaN is never a standard value.

The tables hold only what is entered on the catalog itself (`provenance = 'entered'`): neutral
`catalog_item`s, one `catalog_authorisation` number per country (FR AMM, ES and IT registration
numbers…), the `crop_protection_item` extension (contact/penetrant, downy/powdery), and
`typed_quantity`. A legacy product's `authorisation_no` names no country, so it never becomes an
authorisation. The extension lives in core, not the advisor app, because app models register
through manifests owned elsewhere this wave; contraction (ticket 14) moves it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    column,
    table,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from farmcore.core import units
from farmcore.core.models.base import Base
from farmcore.core.models.canonical import (
    PRODUCT_TARGETS,
    PRODUCT_TYPES,
    _in,
    _now,
    _pk,
    _props,
)

_STANDARD = " OR ".join(
    f"(measure = '{measure}' AND unit = '{unit}')" for measure, unit in units.STANDARD_UNITS.items()
)


class UnitDefinition(Base):
    """Frozen copy of the runtime registry: every accepted unit string, its measure and the
    factor into the standard unit. A parity test keeps it equal to `farmcore.core.units`."""

    __tablename__ = "unit_definition"
    __table_args__ = (
        CheckConstraint(_STANDARD, name="standard"),
        CheckConstraint("factor > 0", name="factor"),
    )

    entered: Mapped[str] = mapped_column(Text, primary_key=True)
    measure: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text)
    factor: Mapped[Decimal] = mapped_column(Numeric)


class CatalogItem(Base):
    __tablename__ = "catalog_item"
    __table_args__ = (CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="name"),)

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text)
    props: Mapped[dict[str, Any]] = _props()
    created_at: Mapped[datetime] = _now()


class CatalogAuthorisation(Base):
    __tablename__ = "catalog_authorisation"
    __table_args__ = (
        UniqueConstraint("catalog_item_id", "country"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="country"),
        CheckConstraint("length(btrim(number)) BETWEEN 1 AND 64", name="number"),
    )

    id: Mapped[int] = _pk()
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id", ondelete="CASCADE")
    )
    country: Mapped[str] = mapped_column(Text)  # ISO 3166-1 alpha-2
    number: Mapped[str] = mapped_column(Text)


class CropProtectionItem(Base):
    """The crop-protection extension of a catalog item."""

    __tablename__ = "crop_protection_item"
    __table_args__ = (
        CheckConstraint(_in("product_type", PRODUCT_TYPES), name="product_type"),
        CheckConstraint(
            "targets <@ ARRAY[" + ", ".join(repr(t) for t in PRODUCT_TARGETS) + "]::text[]",
            name="targets",
        ),
    )

    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id", ondelete="CASCADE"), primary_key=True
    )
    product_type: Mapped[str] = mapped_column(Text)
    targets: Mapped[list[str]] = mapped_column(ARRAY(Text))


class TypedQuantity(Base):
    """An entered quantity: measure/value/unit in the standard unit (all null when the entered
    unit has none), plus the entered value and unit, a label and the provenance."""

    __tablename__ = "typed_quantity"
    __table_args__ = (
        CheckConstraint(
            f"(measure IS NULL AND value IS NULL AND unit IS NULL) OR {_STANDARD}",
            name="standard",
        ),
        CheckConstraint("value IS NULL OR value <> 'NaN'::numeric", name="value"),
        CheckConstraint("entered_value <> 'NaN'::numeric", name="entered_value"),
        CheckConstraint("length(btrim(entered_unit)) > 0", name="entered_unit"),
        CheckConstraint("provenance = 'entered'", name="provenance"),
        Index("ix_typed_quantity_log_id", "log_id"),
    )

    id: Mapped[int] = _pk()
    log_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("log.id", ondelete="CASCADE"))
    catalog_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id"), nullable=True
    )
    measure: Mapped[str | None] = mapped_column(Text)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    entered_value: Mapped[Decimal] = mapped_column(Numeric)
    entered_unit: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text, server_default="entered", default="entered")


# Read-only handles on the legacy views (core_0006); not part of the ORM metadata.
legacy_catalog_item = table(
    "legacy_catalog_item",
    column("product_id"),
    column("name"),
    column("product_type"),
    column("targets"),
    column("provenance"),
)
legacy_typed_quantity = table(
    "legacy_typed_quantity",
    column("legacy_quantity_id"),
    column("log_id"),
    column("product_id"),
    column("measure"),
    column("value"),
    column("unit"),
    column("entered_value"),
    column("entered_unit"),
    column("label"),
    column("provenance"),
)


def enter_quantity(
    session: Session,
    *,
    log_id: int,
    value: Decimal,
    unit: str,
    catalog_item_id: int | None = None,
    label: str | None = None,
) -> TypedQuantity:
    """Store an entered quantity. A unit outside the registry keeps a null standard value; a
    float or non-finite value raises `units.UnitError`, never a silent null."""
    units.decimal_value(value)
    try:
        typed = units.measure(value, unit)
        standard = {"measure": typed.measure, "value": typed.value, "unit": typed.unit}
    except units.UnknownUnit:
        standard = {"measure": None, "value": None, "unit": None}
    row = TypedQuantity(
        log_id=log_id,
        catalog_item_id=catalog_item_id,
        entered_value=value,
        entered_unit=unit,
        label=label,
        **standard,
    )
    session.add(row)
    session.flush()
    return row
