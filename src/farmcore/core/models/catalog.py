"""Neutral catalog items and typed quantities, beside the legacy spray data (migration core_0006).

Expand step: `product` and `quantity` stay the source every consumer reads. Each product is
mirrored as a `catalog_item` with a `crop_protection_item` extension (contact/penetrant,
downy/powdery), and each quantity as a `typed_quantity` that keeps the entered value and unit
and adds the standard value (`farmcore.core.units`). The mirror is written in the same flush as
the legacy row (`_mirror_legacy`), so the two never drift; a unit outside the registry keeps a
null standard value rather than a guessed one.

`catalog_authorisation` holds one authorisation number per country (FR AMM, ES and IT
registration numbers…). A legacy product's `authorisation_no` names no country, so it is never
copied there. The crop-protection extension lives here, not in the advisor app, because app
models register through manifests owned elsewhere; contraction (ticket 14) moves it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import chain
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
    event,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from farmcore.core import units
from farmcore.core.models.base import Base
from farmcore.core.models.canonical import (
    PRODUCT_TARGETS,
    PRODUCT_TYPES,
    Product,
    Quantity,
    _in,
    _now,
    _pk,
    _props,
)

PROVENANCES = ("legacy_quantity", "entered")
_STANDARD = " OR ".join(
    f"(measure = '{measure}' AND unit = '{unit}')" for measure, unit in units.STANDARD_UNITS.items()
)


class CatalogItem(Base):
    __tablename__ = "catalog_item"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="name"),
        UniqueConstraint("product_id"),
    )

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text)
    # The legacy product this item mirrors; None for items created on the catalog alone.
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product.id", ondelete="SET NULL"), nullable=True
    )
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
    """measure/value/unit in the standard unit (all null when the entered unit has none), plus
    the entered value and unit, a label and the provenance."""

    __tablename__ = "typed_quantity"
    __table_args__ = (
        CheckConstraint(
            f"(measure IS NULL AND value IS NULL AND unit IS NULL) OR {_STANDARD}", name="standard"
        ),
        CheckConstraint("value IS NULL OR value::text <> 'NaN'", name="value"),
        CheckConstraint("entered_value::text <> 'NaN'", name="entered_value"),
        CheckConstraint("length(btrim(entered_unit)) > 0", name="entered_unit"),
        CheckConstraint(_in("provenance", PROVENANCES), name="provenance"),
        UniqueConstraint("legacy_quantity_id"),
        Index("ix_typed_quantity_log_id", "log_id"),
    )

    id: Mapped[int] = _pk()
    log_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("log.id", ondelete="CASCADE"))
    catalog_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id"), nullable=True
    )
    legacy_quantity_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("quantity.id", ondelete="CASCADE"), nullable=True
    )
    measure: Mapped[str | None] = mapped_column(Text)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    entered_value: Mapped[Decimal] = mapped_column(Numeric)
    entered_unit: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text)


def typed(value: Any, unit: str) -> dict[str, Any]:
    """Standard measure/value/unit for an entered value, or nulls when it does not type."""
    try:
        q = units.measure(value, unit)
    except units.UnitError:
        return {"measure": None, "value": None, "unit": None}
    return {"measure": q.measure, "value": q.value, "unit": q.unit}


def _mirror_product(connection, product: Product) -> None:
    item_id = connection.execute(
        insert(CatalogItem.__table__)
        .values(name=product.name, product_id=product.id)
        .on_conflict_do_update(index_elements=["product_id"], set_={"name": product.name})
        .returning(CatalogItem.__table__.c.id)
    ).scalar_one()
    values = {"product_type": product.product_type, "targets": list(product.targets or [])}
    connection.execute(
        insert(CropProtectionItem.__table__)
        .values(catalog_item_id=item_id, **values)
        .on_conflict_do_update(index_elements=["catalog_item_id"], set_=values)
    )


def _mirror_quantity(connection, quantity: Quantity) -> None:
    # A quantity removed from `log.quantities` is deleted as an orphan in this flush but can
    # still be listed as dirty; its row is gone and its mirror went with it (cascade).
    kept = connection.scalar(
        select(Quantity.__table__.c.id).where(Quantity.__table__.c.id == quantity.id)
    )
    if kept is None:
        return
    item = (
        None
        if quantity.product_id is None
        else select(CatalogItem.__table__.c.id)
        .where(CatalogItem.__table__.c.product_id == quantity.product_id)
        .scalar_subquery()
    )
    values = {
        "log_id": quantity.log_id,
        "catalog_item_id": item,
        "entered_value": quantity.value,
        "entered_unit": quantity.unit,
        "label": quantity.label,
        "provenance": "legacy_quantity",
        **typed(quantity.value, quantity.unit),
    }
    connection.execute(
        insert(TypedQuantity.__table__)
        .values(legacy_quantity_id=quantity.id, **values)
        .on_conflict_do_update(index_elements=["legacy_quantity_id"], set_=values)
    )


@event.listens_for(Session, "after_flush")
def _mirror_legacy(session: Session, _context) -> None:
    """Mirror products and quantities written in this flush (deleted quantities cascade)."""
    written = [o for o in chain(session.new, session.dirty) if o not in session.deleted]
    products = [o for o in written if isinstance(o, Product)]
    quantities = [o for o in written if isinstance(o, Quantity)]
    if not products and not quantities:
        return
    connection = session.connection()
    for product in products:
        _mirror_product(connection, product)
    for quantity in quantities:
        _mirror_quantity(connection, quantity)
