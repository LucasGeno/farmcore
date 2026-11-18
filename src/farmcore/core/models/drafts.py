"""Proposed writes awaiting the farmer's review (`core/drafts.py`)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base

DRAFT_STATUSES = ("open", "confirmed", "cancelled", "expired")


class Draft(Base):
    """One proposed write: who proposed it (`actor`: `identity:<id>`, `operator`, `token:<id>`,
    `chat:<id>`), the kind that validates and confirms it, the validated payload and where it came
    from. `revision` grows on every edit; a confirm names the revision the farmer reviewed.
    `reviewer_identity_id` is the owner who issued a `token:<id>` actor's token, stored at propose
    time so review never depends on `access_token` (a restore does not load it)."""

    __tablename__ = "draft"
    __table_args__ = (
        UniqueConstraint("farm_id", "actor", "request_key"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in DRAFT_STATUSES) + ")", name="status"
        ),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint("btrim(actor) <> ''", name="actor"),
        CheckConstraint("btrim(request_key) <> ''", name="request_key"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload"),
        CheckConstraint("jsonb_typeof(provenance) = 'object'", name="provenance"),
        CheckConstraint(
            "(status = 'open' AND finalized_at IS NULL AND finalized_by IS NULL) "
            "OR (status <> 'open' AND finalized_at IS NOT NULL)",
            name="terminal",
        ),
        CheckConstraint(
            "(status = 'confirmed') = (result_id IS NOT NULL AND result_model IS NOT NULL)",
            name="result",
        ),
        Index("ix_draft_open_expiry", "expires_at", postgresql_where="status = 'open'"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farm.id"), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    reviewer_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_identity.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_by: Mapped[str | None] = mapped_column(String(128))
    result_model: Mapped[str | None] = mapped_column(String(32))
    result_id: Mapped[int | None] = mapped_column(BigInteger)
