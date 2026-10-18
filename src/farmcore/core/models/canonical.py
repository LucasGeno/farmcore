"""Canonical farm model (farmOS-shaped). Every core table lives here; migration core_0002.

Conventions:
- ids are BIGINT identity columns; every timestamp is `timestamptz` (store UTC).
- enums are `text` + CHECK constraints (no PG enum types: easier to extend).
- `props JSONB NOT NULL DEFAULT '{}'` carries app data; apps validate it with their own schema.
- indexes are declared here so `alembic check` stays clean; geometry columns opt out of
  GeoAlchemy's implicit index and declare an explicit GiST index instead.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from farmcore.core.models.base import Base

ASSET_KINDS = ("station", "sprayer", "regional")
PRODUCT_TYPES = ("contact", "penetrant")
PRODUCT_TARGETS = ("downy", "powdery")
LOG_TYPES = ("spray", "fertilise", "irrigate", "harvest", "observation")
LOG_STATUSES = ("pending", "done")
QUANTITY_MEASURES = ("rate", "volume", "area")
LOG_SOURCE_TYPES = ("voice", "seed", "isoxml", "file")
ADVICE_DECISIONS = ("spray", "wait", "protected", "none")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _props() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)


def _jsonb_list() -> Mapped[list[Any]]:
    return mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True)


def _fk(target: str, *, ondelete: str | None = None, nullable: bool = False) -> Any:
    return mapped_column(BigInteger, ForeignKey(target, ondelete=ondelete), nullable=nullable)


class Farm(Base):
    __tablename__ = "farm"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text)
    props: Mapped[dict[str, Any]] = _props()
    created_at: Mapped[datetime] = _now()


class Parcel(Base):
    __tablename__ = "parcel"
    __table_args__ = (
        UniqueConstraint("farm_id", "alias"),
        Index("ix_parcel_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[int] = _pk()
    farm_id: Mapped[int] = _fk("farm.id")
    name: Mapped[str] = mapped_column(Text)
    alias: Mapped[str] = mapped_column(Text)
    # Nullable: synthetic layouts have no IACS/LPIS reference.
    iacs_id: Mapped[str | None] = mapped_column(Text)
    area_ha: Mapped[Decimal] = mapped_column(Numeric)
    geom: Mapped[WKBElement] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False
    )
    props: Mapped[dict[str, Any]] = _props()


class Asset(Base):
    __tablename__ = "asset"
    __table_args__ = (
        CheckConstraint(_in("kind", ASSET_KINDS), name="kind"),
        Index("ix_asset_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[int] = _pk()
    farm_id: Mapped[int] = _fk("farm.id")
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    geom: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )
    props: Mapped[dict[str, Any]] = _props()


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (
        CheckConstraint(_in("product_type", PRODUCT_TYPES), name="product_type"),
        CheckConstraint(
            "targets <@ ARRAY[" + ", ".join(repr(t) for t in PRODUCT_TARGETS) + "]::text[]",
            name="targets",
        ),
    )

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text)
    authorisation_no: Mapped[str] = mapped_column(Text)
    active_substance: Mapped[str] = mapped_column(Text)
    product_type: Mapped[str] = mapped_column(Text)
    targets: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]"), default=list
    )
    props: Mapped[dict[str, Any]] = _props()


class LogParcel(Base):
    __tablename__ = "log_parcel"

    log_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("log.id", ondelete="CASCADE"), primary_key=True
    )
    parcel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("parcel.id"), primary_key=True)


class Log(Base):
    __tablename__ = "log"
    __table_args__ = (
        CheckConstraint(_in("type", LOG_TYPES), name="type"),
        CheckConstraint(_in("status", LOG_STATUSES), name="status"),
        Index("ix_log_farm_id_type_ts_start", "farm_id", "type", "ts_start"),
    )

    id: Mapped[int] = _pk()
    farm_id: Mapped[int] = _fk("farm.id")
    type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    ts_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bbch: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, server_default="", default="")
    props: Mapped[dict[str, Any]] = _props()
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    parcels: Mapped[list[Parcel]] = relationship(secondary="log_parcel", passive_deletes=True)
    quantities: Mapped[list[Quantity]] = relationship(
        back_populates="log", cascade="all, delete-orphan", passive_deletes=True
    )
    sources: Mapped[list[LogSource]] = relationship(
        back_populates="log", cascade="all, delete-orphan", passive_deletes=True
    )


class Quantity(Base):
    __tablename__ = "quantity"
    __table_args__ = (CheckConstraint(_in("measure", QUANTITY_MEASURES), name="measure"),)

    id: Mapped[int] = _pk()
    log_id: Mapped[int] = _fk("log.id", ondelete="CASCADE")
    product_id: Mapped[int | None] = _fk("product.id", nullable=True)
    measure: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Numeric)
    unit: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)

    log: Mapped[Log] = relationship(back_populates="quantities")
    product: Mapped[Product | None] = relationship()


class Observation(Base):
    """Hourly station values. Plain table (upgrade path: Timescale hypertable, same schema)."""

    __tablename__ = "observation"
    __table_args__ = (
        UniqueConstraint("asset_id", "ts", "variable"),
        Index("ix_observation_ts", "ts", postgresql_using="brin"),
    )

    id: Mapped[int] = _pk()
    asset_id: Mapped[int] = _fk("asset.id")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    variable: Mapped[str] = mapped_column(Text)
    value: Mapped[float] = mapped_column(Double)


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    licence: Mapped[str] = mapped_column(Text)
    props: Mapped[dict[str, Any]] = _props()


class LogSource(Base):
    __tablename__ = "log_source"
    __table_args__ = (CheckConstraint(_in("source_type", LOG_SOURCE_TYPES), name="source_type"),)

    id: Mapped[int] = _pk()
    log_id: Mapped[int] = _fk("log.id", ondelete="CASCADE")
    source_type: Mapped[str] = mapped_column(Text)
    external_key: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any]] = _props()
    confidence: Mapped[float | None] = mapped_column(Float)
    precision_rank: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = _now()

    log: Mapped[Log] = relationship(back_populates="sources")


class ExtId(Base):
    """External key → canonical row. `source` is a source code (e.g. `farmcore-demo`)."""

    __tablename__ = "ext_id"
    __table_args__ = (UniqueConstraint("source", "key"),)

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    res_id: Mapped[int] = mapped_column(BigInteger)


class DataGrant(Base):
    __tablename__ = "data_grant"

    id: Mapped[int] = _pk()
    farm_id: Mapped[int] = _fk("farm.id")
    grantee: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    purpose: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = _now()
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccessLog(Base):
    __tablename__ = "access_log"
    __table_args__ = (Index("ix_access_log_farm_id_ts", "farm_id", "ts"),)

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = _now()
    actor: Mapped[str] = mapped_column(Text)
    farm_id: Mapped[int] = _fk("farm.id")
    grant_id: Mapped[int | None] = _fk("data_grant.id", nullable=True)
    resource: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)


class Advice(Base):
    __tablename__ = "advice"
    __table_args__ = (CheckConstraint(_in("decision", ADVICE_DECISIONS), name="decision"),)

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


Index("ix_advice_parcel_id_evaluated_at", Advice.parcel_id, Advice.evaluated_at.desc())


class Event(Base):
    """Timeline entry. `occurred_at` is clock time (replay-aware); `created_at` is wall time."""

    __tablename__ = "event"

    id: Mapped[int] = _pk()
    farm_id: Mapped[int] = _fk("farm.id")
    res_model: Mapped[str] = mapped_column(Text)
    res_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = _props()
    actor: Mapped[str] = mapped_column(Text)
    source_app: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


Index(
    "ix_event_res_model_res_id_occurred_at",
    Event.res_model,
    Event.res_id,
    Event.occurred_at.desc(),
)


class LlmAudit(Base):
    __tablename__ = "llm_audit"

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = _now()
    adapter: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text)
    request_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ref: Mapped[str | None] = mapped_column(Text)
