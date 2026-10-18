"""Kernel tables: installed apps and the shared clock state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base


class AppModule(Base):
    __tablename__ = "app_module"
    __table_args__ = (CheckConstraint("state IN ('installed', 'uninstalled')", name="state"),)

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), server_default="installed")
    version: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClockState(Base):
    """Single row (id = 1) shared by the web, bot and CLI processes."""

    __tablename__ = "clock_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("mode IN ('replay', 'live')", name="mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    mode: Mapped[str] = mapped_column(String(16), server_default="replay")
    replay_now: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_runs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
