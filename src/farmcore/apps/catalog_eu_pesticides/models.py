"""The EU active-substance register and the cross-catalog substance link (migration
`catalog_eu_0001`).

- `catalog_substance`: one row per active substance the EU Pesticides Database lists, keyed on the
  normalised `substance_key` (see `substances.py`). It holds the substance's reference name, CAS
  number, approval status and approval/expiry dates, with the remaining source fields (category,
  rapporteur member state, low-risk / candidate-for-substitution / basic / micro-organism flags,
  CLP classification and the source's numeric id) in `props`. The source repeats a substance once
  per national authorisation, so exact duplicates collapse onto one row. A substance is never
  deleted: products from every country keep referencing it by key, and historical records keep
  reading its approval status.
- `catalog_item_substance`: the link a country catalog writes for each product's substance, keyed
  on `(catalog_item_id, substance_key)`. `catalog_fr_ephy` and `catalog_it_salute` write it beside
  their neutral `catalog_item` rows, so one query joins any product to its EU approval status by
  key. `name` is the substance as the source wrote it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base
from farmcore.core.models.canonical import _pk, _props

SUBSTANCE_STATES = ("approved", "not_approved", "pending")
_SUBSTANCE_STATE_SQL = ", ".join(repr(s) for s in SUBSTANCE_STATES)


class CatalogSubstance(Base):
    __tablename__ = "catalog_substance"
    __table_args__ = (
        UniqueConstraint("substance_key"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="name"),
        CheckConstraint(f"status IN ({_SUBSTANCE_STATE_SQL})", name="status"),
    )

    id: Mapped[int] = _pk()
    substance_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    cas_number: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    approval_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    props: Mapped[dict[str, Any]] = _props()


class CatalogItemSubstance(Base):
    __tablename__ = "catalog_item_substance"
    __table_args__ = (
        UniqueConstraint("catalog_item_id", "substance_key"),
        CheckConstraint("length(btrim(substance_key)) BETWEEN 1 AND 200", name="substance_key"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="name"),
        Index("ix_catalog_item_substance_substance_key", "substance_key"),
    )

    id: Mapped[int] = _pk()
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id", ondelete="CASCADE")
    )
    substance_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
