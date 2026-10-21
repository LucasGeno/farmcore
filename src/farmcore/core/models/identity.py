"""Local identities are independent of legacy grants and installation operators."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base


class LocalIdentity(Base):
    __tablename__ = "local_identity"
    __table_args__ = (CheckConstraint("login ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'", name="login"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    login: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FarmMembership(Base):
    __tablename__ = "farm_membership"
    __table_args__ = (
        UniqueConstraint("identity_id", "farm_id"),
        CheckConstraint("role IN ('owner','advisor')", name="role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("local_identity.id"), index=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farm.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LocalSession(Base):
    __tablename__ = "local_session"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash"),
        CheckConstraint("csrf_hash ~ '^[0-9a-f]{64}$'", name="csrf_hash"),
        CheckConstraint("expires_at > created_at", name="expiry"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("local_identity.id"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginBucket(Base):
    __tablename__ = "login_bucket"
    __table_args__ = (
        CheckConstraint("dimension IN ('account','peer','global')", name="dimension"),
        CheckConstraint("slot BETWEEN 0 AND 1023", name="slot"),
        CheckConstraint("dimension != 'global' OR slot = 0", name="global_slot"),
        CheckConstraint("attempts BETWEEN 0 AND 100", name="attempts"),
    )

    dimension: Mapped[str] = mapped_column(String(16), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
