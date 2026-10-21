"""Retained operation intent and terminal correction receipts."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base


class OperationCorrection(Base):
    __tablename__ = "operation_correction"
    __table_args__ = (
        UniqueConstraint("farm_id", "actor_key", "request_key"),
        CheckConstraint("status IN ('pending','confirmed','cancelled')", name="status"),
        CheckConstraint("btrim(reason) <> ''", name="reason"),
        CheckConstraint("btrim(request_key) <> ''", name="request_key"),
        CheckConstraint("btrim(actor_label) <> ''", name="actor_label"),
        CheckConstraint("jsonb_typeof(before_snapshot) = 'object'", name="before_snapshot"),
        CheckConstraint("jsonb_typeof(after_snapshot) = 'object'", name="after_snapshot"),
        CheckConstraint("btrim(actor_key) <> ''", name="actor_key"),
        CheckConstraint("base_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint"),
        CheckConstraint(
            "(status = 'pending' AND finalized_at IS NULL "
            "AND finalized_actor_key IS NULL AND finalized_actor_label IS NULL) "
            "OR (status <> 'pending' AND finalized_at IS NOT NULL "
            "AND finalized_actor_key IS NOT NULL AND finalized_actor_label IS NOT NULL)",
            name="terminal",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farm.id"), index=True)
    original_log_id: Mapped[int] = mapped_column(BigInteger)
    live_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("log.id", ondelete="SET NULL"), index=True
    )
    request_key: Mapped[str] = mapped_column(String(128))
    actor_key: Mapped[str] = mapped_column(String(128))
    actor_label: Mapped[str] = mapped_column(Text)
    creator_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_identity.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    reason: Mapped[str] = mapped_column(Text)
    base_fingerprint: Mapped[str] = mapped_column(String(64))
    before_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    after_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_actor_key: Mapped[str | None] = mapped_column(Text)
    finalized_actor_label: Mapped[str | None] = mapped_column(Text)
