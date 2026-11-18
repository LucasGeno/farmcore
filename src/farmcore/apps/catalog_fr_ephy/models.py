"""France's E-Phy catalog (migration `catalog_fr_0001`): registered uses beside the neutral
catalog items and per-country authorisations, and the import fingerprint that makes a re-import
of an unchanged source a no-op.

The neutral rows (one `catalog_item` per product, one `catalog_authorisation(country='FR',
number=AMM)`) live in `core/models/catalog.py`. This app adds:

- `catalog_use`: one row per E-Phy usage line. `crop` and `target` come from the usage
  identifier (`Vigne*Trt Part.Aer.*Oïdium(s)`); a two-part identifier (herbicides) has no target.
  The dose is kept exactly as E-Phy publishes it (value and unit), never converted: most E-Phy
  dose units are not FarmCore measures. `state` is normalised (`authorised` / `withdrawn` /
  `provisional`); a withdrawn product is flagged, never deleted.
- `catalog_import`: one row per import run — the source, its licence, when it was retrieved and a
  content hash. An unchanged source has the same hash, so the next run writes nothing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

# Normalised use states (the source spells them in French).
USE_STATES = ("authorised", "withdrawn", "provisional")
USE_STATE_SQL = ", ".join(repr(s) for s in USE_STATES)


class CatalogUse(Base):
    __tablename__ = "catalog_use"
    __table_args__ = (
        CheckConstraint("length(btrim(crop)) BETWEEN 1 AND 200", name="crop"),
        CheckConstraint(f"state IN ({USE_STATE_SQL})", name="state"),
        CheckConstraint("max_dose IS NULL OR max_dose > 0", name="max_dose"),
        CheckConstraint("interval_days IS NULL OR interval_days > 0", name="interval_days"),
        CheckConstraint(
            "max_applications IS NULL OR max_applications > 0", name="max_applications"
        ),
        Index("ix_catalog_use_catalog_item_id", "catalog_item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id", ondelete="CASCADE")
    )
    identifiant: Mapped[str] = mapped_column(Text)
    crop: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    max_dose: Mapped[Decimal | None] = mapped_column(Numeric)
    max_dose_unit: Mapped[str | None] = mapped_column(Text)
    interval_days: Mapped[Decimal | None] = mapped_column(Numeric)
    max_applications: Mapped[int | None] = mapped_column(Integer)
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
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(Text)
    product_count: Mapped[int] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
