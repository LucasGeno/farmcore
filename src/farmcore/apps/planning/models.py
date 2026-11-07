"""Planned jobs (migration `planning_0001`) and their resources (migration `planning_0002`).

`planning_job` is intended work frozen from one stored assessment (`advice`) and its parameter
version; `planning_job_parcel` lists its parcels; `planning_job_revision` is the append-only
snapshot history (creation, each edit, cancellation). Every relation is farm-scoped through a
composite `(id, farm_id)` foreign key. Revision updates are refused by a database trigger;
deletes stay possible for demo reset.

`planning_machine` and `planning_operator` are configured resources; NULL capacity is unknown,
never safe. `planning_resource_window` holds explicit availability periods (`available=false`
overrides an overlapping `true`; no rows means unknown). `planning_job_assignment` books one
resource for a job over a half-open `[starts_at, ends_at)` window; exclusion constraints reject
overlapping bookings of the same resource in the database.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

JOB_STATUSES = ("planned", "cancelled")
REVISION_ACTIONS = ("created", "edited", "cancelled")
MACHINE_KINDS = ("sprayer", "tractor", "other")
RESOURCES = ("machine", "operator")


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


def _audit_checks() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint("length(btrim(created_by)) > 0", name="created_by"),
        CheckConstraint("length(btrim(updated_by)) > 0", name="updated_by"),
    )


def _resource_fks(ondelete: str | None) -> tuple[ForeignKeyConstraint, ...]:
    return tuple(
        ForeignKeyConstraint(
            (f"{kind}_id", "farm_id"),
            (f"planning_{kind}.id", f"planning_{kind}.farm_id"),
            ondelete=ondelete,
        )
        for kind in RESOURCES
    )


class _Audited:
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlanningMachine(_Audited, Base):
    __tablename__ = "planning_machine"
    __table_args__ = (
        UniqueConstraint("id", "farm_id"),
        UniqueConstraint("farm_id", "name"),
        CheckConstraint(_in("kind", MACHINE_KINDS), name="kind"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 120", name="name"),
        CheckConstraint(
            "work_rate_ha_per_hour IS NULL OR work_rate_ha_per_hour > 0", name="work_rate"
        ),
        CheckConstraint("tank_litres IS NULL OR tank_litres > 0", name="tank_litres"),
        CheckConstraint("length(notes) <= 2000", name="notes"),
        *_audit_checks(),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default="sprayer", default="sprayer")
    # Hectares per productive hour; NULL = not configured (unknown, never assumed safe).
    work_rate_ha_per_hour: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    # Stored configuration only; never converted into a dose or coverage claim.
    tank_litres: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    notes: Mapped[str] = mapped_column(Text, server_default="", default="")


class PlanningOperator(_Audited, Base):
    __tablename__ = "planning_operator"
    __table_args__ = (
        UniqueConstraint("id", "farm_id"),
        UniqueConstraint("farm_id", "name"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 120", name="name"),
        CheckConstraint("length(notes) <= 2000", name="notes"),
        *_audit_checks(),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    notes: Mapped[str] = mapped_column(Text, server_default="", default="")


class PlanningResourceWindow(_Audited, Base):
    __tablename__ = "planning_resource_window"
    __table_args__ = (
        *_resource_fks("CASCADE"),
        CheckConstraint("num_nonnulls(machine_id, operator_id) = 1", name="resource"),
        CheckConstraint("starts_at < ends_at", name="range"),
        CheckConstraint("length(notes) <= 2000", name="notes"),
        *_audit_checks(),
        *(
            Index(
                f"ix_planning_resource_window_{kind}",
                "farm_id",
                f"{kind}_id",
                "starts_at",
                "ends_at",
                postgresql_where=text(f"{kind}_id IS NOT NULL"),
            )
            for kind in RESOURCES
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    machine_id: Mapped[int | None] = mapped_column(BigInteger)
    operator_id: Mapped[int | None] = mapped_column(BigInteger)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[str] = mapped_column(Text, server_default="", default="")


class PlanningJobAssignment(_Audited, Base):
    __tablename__ = "planning_job_assignment"
    __table_args__ = (
        ForeignKeyConstraint(
            ("job_id", "farm_id"),
            ("planning_job.id", "planning_job.farm_id"),
            ondelete="CASCADE",
        ),
        *_resource_fks(None),
        CheckConstraint("num_nonnulls(machine_id, operator_id) = 1", name="resource"),
        CheckConstraint("starts_at < ends_at", name="range"),
        *_audit_checks(),
        # Overlapping half-open bookings of one resource raise 23P01 (needs btree_gist).
        *(
            ExcludeConstraint(
                ("farm_id", "="),
                (f"{kind}_id", "="),
                (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
                name=f"ex_planning_job_assignment_{kind}",
                using="gist",
                where=text(f"{kind}_id IS NOT NULL"),
            )
            for kind in RESOURCES
        ),
        # At most one machine and one operator per job.
        *(
            Index(
                f"ix_planning_job_assignment_job_{kind}",
                "job_id",
                unique=True,
                postgresql_where=text(f"{kind}_id IS NOT NULL"),
            )
            for kind in RESOURCES
        ),
        Index("ix_planning_job_assignment_job", "job_id"),
        Index("ix_planning_job_assignment_farm_start", "farm_id", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    job_id: Mapped[int] = mapped_column(BigInteger)
    machine_id: Mapped[int | None] = mapped_column(BigInteger)
    operator_id: Mapped[int | None] = mapped_column(BigInteger)
    # Denormalised from the job revision it books: [scheduled_start, + duration).
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
