"""Crop protection's advice table (core_0002, core_0012): one stored evaluation per parcel and
time, projecting its assessment envelope (`core/assessments.py`).

The table keeps its name `advice` and its resource scope (`advice:read`); the model and its
decisions are crop protection's, not core's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from farmcore.core.models.base import Base
from farmcore.core.models.canonical import _fk, _in, _jsonb_list, _now, _pk, _props

ADVICE_DECISIONS = ("spray", "wait", "protected", "none")


class Advice(Base):
    __tablename__ = "advice"
    __table_args__ = (
        CheckConstraint(_in("decision", ADVICE_DECISIONS), name="decision"),
        # Target of farm-scoped composite foreign keys (planning_0001).
        UniqueConstraint("id", "farm_id"),
        # The envelope this row projects (core_0012): same farm, parcel and evaluation time.
        UniqueConstraint("assessment_id"),
        ForeignKeyConstraint(
            ("assessment_id", "farm_id", "parcel_id", "evaluated_at"),
            (
                "assessment.id",
                "assessment.farm_id",
                "assessment.parcel_id",
                "assessment.evaluated_at",
            ),
        ),
    )

    id: Mapped[int] = _pk()
    farm_id: Mapped[int] = _fk("farm.id")
    parcel_id: Mapped[int] = _fk("parcel.id")
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(Text)
    reasons: Mapped[list[Any]] = _jsonb_list()
    next_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    protected_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    products: Mapped[list[Any]] = _jsonb_list()
    props: Mapped[dict[str, Any]] = _props()
    created_at: Mapped[datetime] = _now()
    # Crop protection's typed result and its evidence (`core/assessments.py`); None on rows
    # saved before core_0012, whose evidence stays in `props`.
    assessment_id: Mapped[int | None] = mapped_column(BigInteger)
    assessment: Mapped[Any] = relationship(
        "Assessment",
        primaryjoin="Advice.assessment_id == Assessment.id",
        foreign_keys=[assessment_id],
        lazy="raise",
    )


Index("ix_advice_parcel_id_evaluated_at", Advice.parcel_id, Advice.evaluated_at.desc())
