"""Crop protection's table (migration core_0018): the disease-model attributes of a
plant-protection item.

`advisor_product` extends operations' `crop_protection_item`: contact or penetrant, and the
diseases it targets (downy, powdery). An item with no row here gives no cover (the protection and
powdery engines ignore it, as they ignored an unknown product).
"""

from __future__ import annotations

from sqlalchemy import ARRAY, BigInteger, CheckConstraint, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base
from farmcore.core.models.canonical import _in

PRODUCT_TYPES = ("contact", "penetrant")
PRODUCT_TARGETS = ("downy", "powdery")


class AdvisorProduct(Base):
    __tablename__ = "advisor_product"
    __table_args__ = (
        CheckConstraint(_in("product_type", PRODUCT_TYPES), name="product_type"),
        CheckConstraint(
            "targets <@ ARRAY[" + ", ".join(repr(t) for t in PRODUCT_TARGETS) + "]::text[]",
            name="targets",
        ),
    )

    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crop_protection_item.catalog_item_id", ondelete="CASCADE"),
        primary_key=True,
    )
    product_type: Mapped[str] = mapped_column(Text)
    targets: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]"), default=list
    )
