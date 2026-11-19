"""Operations' table (migrations core_0006, core_0018): the plant-protection extension of a
catalog item.

A `crop_protection_item` row says the catalog item is a plant-protection product and holds its
legal record facts (Reg. (EU) 2023/564): the authorisation number as printed (no country, so it
never becomes a `catalog_authorisation`), the active substance, and `props` (placeholder flag,
source, label usage and limits, organic sources) that `checks.Label` and `production` read. The
disease-model attributes (contact/penetrant, downy/powdery) are crop protection's
(`farmcore.apps.advisor.catalog`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base
from farmcore.core.models.canonical import _props


class CropProtectionItem(Base):
    __tablename__ = "crop_protection_item"

    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_item.id", ondelete="CASCADE"), primary_key=True
    )
    authorisation_no: Mapped[str] = mapped_column(Text)
    active_substance: Mapped[str] = mapped_column(Text)
    props: Mapped[dict[str, Any]] = _props()
