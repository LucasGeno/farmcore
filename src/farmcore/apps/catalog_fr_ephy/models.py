"""France's E-Phy catalog (migration `catalog_fr_0001`): registered uses beside the neutral
catalog items and per-country authorisations, and the import fingerprint that makes a re-import
of an unchanged source a no-op.

The neutral rows (one `catalog_item` per product, one `catalog_authorisation(country='FR',
number=AMM)`) live in `core/models/catalog.py`. This app adds:

- `catalog_use`: one row per E-Phy usage line, keyed on `(catalog_item_id, use_key)` so a
  weekly re-import updates a use in place instead of churning its id. `use_key` is a content hash
  of the mapped fields: the source repeats an `identifiant` per product (differing in the harvest
  interval, dose or buffer zones), so the identifiant alone is not unique, and any change to a
  mapped field is a new use. **`catalog_use.id` is therefore not a stable reference**: a record
  must snapshot the use fields it needs (identifiant, dose and unit, state, harvest interval,
  buffer zones) and FK `catalog_item` (AMM-keyed, never deleted), never `catalog_use.id`. `crop`
  and `target` come from the usage identifier (`Vigne*Trt Part.Aer.*Oïdium(s)`); a two-part
  identifier (herbicides) has no target. The dose is kept exactly as E-Phy publishes it (value
  and unit), never converted: most E-Phy dose units are not FarmCore measures. `state` is
  normalised (`authorised` / `withdrawn` / `provisional`); a withdrawn product is flagged, never
  deleted.
- `catalog_import`: one row per import run — the source, its licence, attribution, the resource
  URL and archive date, and a content hash. An unchanged source has the same hash, so the next
  run writes nothing.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

# Normalised use states (the source spells them in French).
USE_STATES = ("authorised", "withdrawn", "provisional")
USE_STATE_SQL = ", ".join(repr(s) for s in USE_STATES)


class CatalogUse(Base):
    __tablename__ = "catalog_use"
    __table_args__ = (
        UniqueConstraint("catalog_item_id", "use_key"),
        CheckConstraint("length(btrim(crop)) BETWEEN 1 AND 200", name="crop"),
        CheckConstraint(f"state IN ({USE_STATE_SQL})", name="state"),
        CheckConstraint("max_dose IS NULL OR max_dose > 0", name="max_dose"),
        CheckConstraint("interval_days IS NULL OR interval_days > 0", name="interval_days"),
        CheckConstraint(
            "max_applications IS NULL OR max_applications > 0", name="max_applications"
        ),
        CheckConstraint("phi_days IS NULL OR phi_days > 0", name="phi_days"),
        Index("ix_catalog_use_catalog_item_id", "catalog_item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id", ondelete="CASCADE")
    )
    use_key: Mapped[str] = mapped_column(Text)
    identifiant: Mapped[str] = mapped_column(Text)
    crop: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    max_dose: Mapped[Decimal | None] = mapped_column(Numeric)
    max_dose_unit: Mapped[str | None] = mapped_column(Text)
    interval_days: Mapped[Decimal | None] = mapped_column(Numeric)
    max_applications: Mapped[int | None] = mapped_column(Integer)
    phi_days: Mapped[int | None] = mapped_column(Integer)
    distribution_end: Mapped[date | None] = mapped_column(Date)
    use_end: Mapped[date | None] = mapped_column(Date)
    znt_aquatic: Mapped[Decimal | None] = mapped_column(Numeric)
    znt_arthropods: Mapped[Decimal | None] = mapped_column(Numeric)
    znt_plants: Mapped[Decimal | None] = mapped_column(Numeric)
    state: Mapped[str] = mapped_column(Text)


class CatalogImport(Base):
    __tablename__ = "catalog_import"
    __table_args__ = (
        UniqueConstraint("source", "content_hash"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
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
