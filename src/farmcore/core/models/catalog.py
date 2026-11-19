"""Neutral catalog items and typed quantities (core_0006), the single copy of spray data since
core_0018.

The tables hold what is entered on the catalog itself (`provenance = 'entered'`): neutral
`catalog_item`s, one `catalog_authorisation` number per country (FR AMM, ES and IT registration
numbers…), and `typed_quantity`: an amount as entered (value, unit and, for a spray's dose, the
`rate` it was entered as) with the standard measure/value/unit from the runtime registry, null
when the unit is unknown. Extensions belong to their apps: operations' `crop_protection_item`
(a plant-protection product's legal label facts), crop protection's `advisor_product`.

core_0018 moved every legacy `product` and `quantity` row here and froze both tables (kept only
for its rollback). `legacy_product_map` and `legacy_quantity_map` record where each went: the only
way an id kept in historic JSON or a published id resolves. A legacy `authorisation_no` names no
country, so it never becomes a `catalog_authorisation`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from farmcore.core import units
from farmcore.core.models.base import Base
from farmcore.core.models.canonical import _in, _now, _pk, _props

# How an amount was entered (farmOS quantity measures).
ENTERED_MEASURES = ("rate", "volume", "area")

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


class CatalogImport(Base):
    """One import run's fingerprint and provenance, shared by every catalog import module.

    `source` names the module (`ephy-fr`, `eu-pesticides`, `it-salute`); `content_hash` is the
    whole-file hash, so an unchanged source has the same hash and the next run writes nothing.
    `product_count`/`use_count` are the module's own meaning of the two counts (a substance
    register stores its substances in `product_count` and 0 in `use_count`)."""

    __tablename__ = "catalog_import"
    __table_args__ = (
        UniqueConstraint("source", "content_hash"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash"),
    )

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(Text)
    licence: Mapped[str] = mapped_column(Text)
    licence_id: Mapped[str] = mapped_column(Text)
    attribution: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(Text)
    resource_url: Mapped[str] = mapped_column(Text)
    archive_name: Mapped[str] = mapped_column(Text)
    archive_date: Mapped[str] = mapped_column(Text)
    product_count: Mapped[int] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


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
        CheckConstraint(_in("entered_measure", ENTERED_MEASURES), name="entered_measure"),
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
    # What the amount was entered as (farmOS quantity measure): `rate` for a spray's dose, null
    # for every other entry. Kept apart from the standard `measure`, which an unknown unit nulls.
    entered_measure: Mapped[str | None] = mapped_column(Text)

    log: Mapped[Any] = relationship("Log", back_populates="typed_quantities")
    catalog_item: Mapped[CatalogItem | None] = relationship()


class LegacyProductMap(Base):
    """Frozen by core_0018: each legacy `product` id and the catalog item it became. Written only
    by that migration; the runtime reads it to resolve ids kept in historic JSON (events,
    revisions, corrections, evidence) and published ids (NGSI-LD v1)."""

    __tablename__ = "legacy_product_map"

    product_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id"), unique=True
    )


class LegacyQuantityMap(Base):
    """Frozen by core_0018: each legacy `quantity` id and the typed quantity it became (gone with
    it when a correction replaces the dose)."""

    __tablename__ = "legacy_quantity_map"

    quantity_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    typed_quantity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("typed_quantity.id", ondelete="CASCADE"), unique=True
    )


def typed_quantity(
    *,
    value: Decimal,
    unit: str,
    catalog_item_id: int | None = None,
    label: str | None = None,
    entered_measure: str | None = None,
) -> TypedQuantity:
    """An entered quantity, not yet added. A unit outside the registry keeps a null standard
    value; a float or non-finite value raises `units.UnitError`, never a silent null."""
    units.decimal_value(value)
    try:
        typed = units.measure(value, unit)
        standard = {"measure": typed.measure, "value": typed.value, "unit": typed.unit}
    except units.UnknownUnit:
        standard = {"measure": None, "value": None, "unit": None}
    return TypedQuantity(
        catalog_item_id=catalog_item_id,
        entered_value=value,
        entered_unit=unit,
        label=label,
        entered_measure=entered_measure,
        **standard,
    )


def enter_quantity(
    session: Session,
    *,
    log_id: int,
    value: Decimal,
    unit: str,
    catalog_item_id: int | None = None,
    label: str | None = None,
    entered_measure: str | None = None,
) -> TypedQuantity:
    """Store an entered quantity on a log (see `typed_quantity`)."""
    row = typed_quantity(
        value=value,
        unit=unit,
        catalog_item_id=catalog_item_id,
        label=label,
        entered_measure=entered_measure,
    )
    row.log_id = log_id
    session.add(row)
    session.flush()
    return row
