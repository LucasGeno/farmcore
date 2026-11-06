"""Planned jobs (migration `planning_0001`).

`planning_job` is intended work frozen from one stored assessment (`advice`) and its parameter
version; `planning_job_parcel` lists its parcels; `planning_job_revision` is the append-only
snapshot history (creation, each edit, cancellation). Every relation is farm-scoped through a
composite `(id, farm_id)` foreign key. Revision updates are refused by a database trigger;
deletes stay possible for demo reset.
"""

from __future__ import annotations

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

JOB_STATUSES = ("planned", "cancelled")
REVISION_ACTIONS = ("created", "edited", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PlanningJob(Base):
    __tablename__ = "planning_job"
    __table_args__ = (
        UniqueConstraint("id", "farm_id"),
        ForeignKeyConstraint(("assessment_id", "farm_id"), ("advice.id", "advice.farm_id")),
        ForeignKeyConstraint(
            ("config_version_id", "farm_id"),
            ("parameter_version.id", "parameter_version.farm_id"),
        ),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="name"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 1440", name="duration_minutes"),
        CheckConstraint("length(notes) <= 10000", name="notes"),
        CheckConstraint("length(btrim(created_by)) > 0", name="created_by"),
        CheckConstraint("length(btrim(updated_by)) > 0", name="updated_by"),
        CheckConstraint("length(btrim(config_version_status)) > 0", name="config_version_status"),
        CheckConstraint("jsonb_typeof(props) = 'object'", name="props"),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) "
            "OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)",
            name="cancel_metadata",
        ),
        Index("ix_planning_job_farm_start", "farm_id", "scheduled_start", "id"),
        Index("ix_planning_job_assessment", "assessment_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    status: Mapped[str] = mapped_column(Text, server_default="planned", default="planned")
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"), default=1)
    name: Mapped[str] = mapped_column(Text)
    # The stored advice row this job was planned from, and its parameter version (frozen copy of
    # `advice.props["config_version"]`; id NULL for unversioned advice).
    assessment_id: Mapped[int] = mapped_column(BigInteger)
    config_version_id: Mapped[int | None] = mapped_column(BigInteger)
    config_version_status: Mapped[str] = mapped_column(Text)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("product.id"))
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Work duration; the end is derived (scheduled_start + duration).
    duration_minutes: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )


class PlanningJobParcel(Base):
    __tablename__ = "planning_job_parcel"
    __table_args__ = (
        ForeignKeyConstraint(
            ("job_id", "farm_id"),
            ("planning_job.id", "planning_job.farm_id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(("parcel_id", "farm_id"), ("parcel.id", "parcel.farm_id")),
        Index("ix_planning_job_parcel_parcel", "parcel_id", "job_id"),
    )

    farm_id: Mapped[int] = mapped_column(BigInteger)
    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parcel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class PlanningJobRevision(Base):
    __tablename__ = "planning_job_revision"
    __table_args__ = (
        UniqueConstraint("job_id", "revision"),
        ForeignKeyConstraint(("job_id", "farm_id"), ("planning_job.id", "planning_job.farm_id")),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint(_in("action", REVISION_ACTIONS), name="action"),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot"),
        CheckConstraint("length(btrim(actor)) > 0", name="actor"),
        CheckConstraint("length(reason) <= 2000", name="reason"),
        Index("ix_planning_job_revision_farm_job", "farm_id", "job_id", text("revision DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    job_id: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
