"""Local identities are independent of legacy grants and installation operators."""

from datetime import datetime

from sqlalchemy import (
    ARRAY,
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


class AccessToken(Base):
    """A bearer token bound to one `data_grant`: the grant carries the farm, scopes, validity and
    revocation (so Sharing shows and revokes it like any grant); this row carries who issued it
    and when it was last used. `kind` says what holds it: `personal` (an owner's agent; the row
    keeps the SHA-256 of the token) or `oauth` (an agent the owner approved over OAuth; its
    rotating secrets are `oauth_token` rows, and it is valid only for `audience`, the RFC 8707
    resource it was issued for, from `client_id`). Its id is the agent's audit actor either way."""

    __tablename__ = "access_token"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash"),
        CheckConstraint("kind IN ('personal', 'oauth')", name="kind"),
        CheckConstraint(
            "(kind = 'personal' AND token_hash IS NOT NULL AND client_id IS NULL"
            " AND audience IS NULL) OR (kind = 'oauth' AND token_hash IS NULL"
            " AND client_id IS NOT NULL AND audience IS NOT NULL)",
            name="kind_fields",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    grant_id: Mapped[int] = mapped_column(ForeignKey("data_grant.id"), unique=True)
    created_by_identity_id: Mapped[int] = mapped_column(ForeignKey("local_identity.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_id: Mapped[str | None] = mapped_column(String(255))
    audience: Mapped[str | None] = mapped_column(Text)


class OAuthCode(Base):
    """An authorization code an owner's consent issued (RFC 6749 §4.1.2): its SHA-256, the
    request it answers (client, exact `redirect_uri`, S256 PKCE challenge, resource) and what
    the owner approved (farm, scopes, agent name). Single use: `used_at` is set by the one
    exchange that wins, and `grant_id` names the connection it created, revoked on a replay."""

    __tablename__ = "oauth_code"
    __table_args__ = (CheckConstraint("code_hash ~ '^[0-9a-f]{64}$'", name="code_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[str] = mapped_column(String(255))
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(Text)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farm.id"), index=True)
    created_by_identity_id: Mapped[int] = mapped_column(ForeignKey("local_identity.id"), index=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    grantee: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grant_id: Mapped[int | None] = mapped_column(ForeignKey("data_grant.id"), index=True)


class OAuthToken(Base):
    """One secret of an OAuth connection (`access_token` kind `oauth` on the same grant): an
    `access` token (short-lived) or a `refresh` token (rotated on use). Only its SHA-256 is
    kept; `revoked_at` is set when it is rotated, refreshed past or revoked."""

    __tablename__ = "oauth_token"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash"),
        CheckConstraint("kind IN ('access', 'refresh')", name="kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    grant_id: Mapped[int] = mapped_column(ForeignKey("data_grant.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RegisteredClient(Base):
    """An OAuth client this server knows without the operator listing it (`core/oauth/clients`):
    one `dynamic`ally registered (RFC 7591, from the address slot `peer_slot`, never the
    address) or the last valid client ID metadata document fetched from the URL `client_id`
    (`metadata`, trusted until `fresh_until`, then fetched again). Name and redirect URIs are
    self-reported by the client."""

    __tablename__ = "oauth_client"
    __table_args__ = (
        CheckConstraint("registration IN ('dynamic', 'metadata')", name="registration"),
        CheckConstraint(
            "(registration = 'dynamic' AND fresh_until IS NULL AND client_id !~ '^https://')"
            " OR (registration = 'metadata' AND fresh_until IS NOT NULL AND peer_slot IS NULL)",
            name="registration_fields",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255), unique=True)
    registration: Mapped[str] = mapped_column(String(16))
    client_name: Mapped[str] = mapped_column(String(80))
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text))
    peer_slot: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fresh_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
