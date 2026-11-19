"""The shared assessment evidence envelope (`core/assessments.py`, revision core_0012).

One row is one module's computed result with the evidence it was computed from: the subject
(farm, optional parcel), the evaluation time, the availability status, the configuration version
and the inputs it read. The typed result belongs to its kind (`Manifest.assessment_kinds`); the
envelope never interprets it. Rows are append-only (a trigger refuses UPDATE), so a saved
assessment stays reproducible; a demo reset may still delete them.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

ASSESSMENT_STATUSES = ("evaluable", "limited", "unavailable")


class Assessment(Base):
    __tablename__ = "assessment"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ASSESSMENT_STATUSES) + ")", name="status"
        ),
        CheckConstraint(r"kind ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'", name="kind"),
        CheckConstraint("kind_version >= 1", name="kind_version"),
        *(
            CheckConstraint(f"jsonb_typeof({c}) = 'object'", name=c)
            for c in ("config_version", "input_snapshot", "result")
        ),
        CheckConstraint(
            "effective_config IS NULL OR jsonb_typeof(effective_config) = 'object'",
            name="effective_config",
        ),
        # A parcel subject is a parcel of the same farm.
        ForeignKeyConstraint(("parcel_id", "farm_id"), ("parcel.id", "parcel.farm_id")),
        # Target of a module row that projects this envelope: same farm, parcel and time.
        UniqueConstraint("id", "farm_id", "parcel_id", "evaluated_at"),
        Index("ix_assessment_farm_id_kind_evaluated_at", "farm_id", "kind", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    parcel_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    kind_version: Mapped[int] = mapped_column(Integer)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    # The parameter version identity; `effective_config` only when there is no version to name.
    config_version: Mapped[dict[str, Any]] = mapped_column(JSONB)
    effective_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
