"""Append-only parameter versions, human opinions and activation events.

Update/delete prevention is enforced by the serialized database migration.
Actor labels intentionally survive account deletion and are not identity foreign keys.
"""

from datetime import datetime
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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

SCOPE = ("farm_id", "crop", "region", "evaluation_year")


class ScopedEvent:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    crop: Mapped[str] = mapped_column(Text, default="vine", server_default="vine")
    region: Mapped[str] = mapped_column(Text)
    evaluation_year: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def scope_checks():
    return (
        CheckConstraint("crop = 'vine'", name="crop"),
        CheckConstraint("length(btrim(region)) > 0", name="region"),
        CheckConstraint("evaluation_year BETWEEN 1900 AND 2200", name="evaluation_year"),
    )


class ParameterVersion(ScopedEvent, Base):
    __tablename__ = "parameter_version"
    __table_args__ = (
        *scope_checks(),
        UniqueConstraint("id", *SCOPE),
        ForeignKeyConstraint(
            ("parent_version_id", *SCOPE), tuple(f"parameter_version.{c}" for c in ("id", *SCOPE))
        ),
        CheckConstraint("length(btrim(created_by)) > 0", name="created_by"),
        CheckConstraint("length(btrim(evidence)) > 0", name="evidence"),
        CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="config_hash"),
        *(
            CheckConstraint(f"jsonb_typeof({c}) = 'object'", name=c)
            for c in ("overrides", "parameter_catalog", "effective_config")
        ),
        Index(
            "ix_parameter_version_baseline_scope",
            *SCOPE,
            unique=True,
            postgresql_where=text("baseline"),
        ),
        Index("ix_parameter_version_scope_id", *SCOPE, text("id DESC")),
    )
    parent_version_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameter_catalog: Mapped[dict[str, Any]] = mapped_column(JSONB)
    effective_config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_hash: Mapped[str] = mapped_column(Text)
    baseline: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ParameterReview(ScopedEvent, Base):
    __tablename__ = "parameter_review"
    __table_args__ = (
        *scope_checks(),
        UniqueConstraint("id", "version_id", *SCOPE),
        ForeignKeyConstraint(
            ("version_id", *SCOPE), tuple(f"parameter_version.{c}" for c in ("id", *SCOPE))
        ),
        CheckConstraint("verdict IN ('approved', 'rejected', 'unsure')", name="verdict"),
        *(
            CheckConstraint(f"length(btrim({c})) > 0", name=c)
            for c in ("reviewer", "note", "evidence")
        ),
        Index("ix_parameter_review_version_id", "version_id", text("id DESC")),
    )
    version_id: Mapped[int] = mapped_column(BigInteger)
    reviewer: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)


class ParameterActivation(ScopedEvent, Base):
    __tablename__ = "parameter_activation"
    __table_args__ = (
        *scope_checks(),
        ForeignKeyConstraint(
            ("version_id", *SCOPE), tuple(f"parameter_version.{c}" for c in ("id", *SCOPE))
        ),
        ForeignKeyConstraint(
            ("review_id", "version_id", *SCOPE),
            tuple(f"parameter_review.{c}" for c in ("id", "version_id", *SCOPE)),
        ),
        CheckConstraint("length(btrim(actor)) > 0", name="actor"),
        Index("ix_parameter_activation_scope_id", *SCOPE, text("id DESC")),
    )
    version_id: Mapped[int] = mapped_column(BigInteger)
    review_id: Mapped[int] = mapped_column(BigInteger)
    actor: Mapped[str] = mapped_column(Text)
