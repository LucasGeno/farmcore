"""Normalised weather tables every provider writes (migration core_0008, expand step).

`observation`, `forecast_run` and `forecast_value` stay the source every current consumer reads;
these tables sit beside them until the consumers move (expand, migrate, contract).

- `weather_variable`: frozen reference rows (CF standard name, stored unit, legacy name), written
  only by the migration (`migrate.MIGRATION_OWNED_TABLES`).
- `weather_receipt`: one provider response: provider, dataset, the keyless request, fetch time,
  licence (an SPDX identifier or a named licence; `unknown` when none is known), attribution,
  whether the data may be redistributed, payload hash and library version. An unknown licence
  (`unknown`, `noassertion` or `none`, in any case or padding) is never redistributable: a
  database check, so no writer can bypass it.
- `weather_station`: WIGOS ID (when the station has one), provider ID, location, elevation and
  the owning farm (null: a public network station).
- `weather_observation` / `weather_forecast`: a CF variable, a decimal value in that variable's
  unit (a composite foreign key, so a value in another unit cannot be stored) and its receipt.
  A forecast's `run` is null when the provider does not say when the model was issued; member 0
  is the deterministic (or control) forecast. Both carry the period a value covers
  (`variables.Variable.cell_method` says how); a forecast's period may be unknown (null).
- An observation may be `provisional` (ERA5T): a final value for the same station, variable and
  period is stored beside it (core_0020), never over it, with a `revision_note` saying what it
  replaced. Readers prefer the final value (`weather.store.held`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry, WKBElement
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
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base
from farmcore.core.models.canonical import _pk, _props

UNKNOWN_LICENCE = "unknown"
# Licence values that state no licence (SPDX `NOASSERTION`, `none`), compared case-folded.
UNKNOWN_LICENCES = ("unknown", "noassertion", "none")
UNKNOWN_NOT_REDISTRIBUTED = (
    f"lower(btrim(licence)) NOT IN {UNKNOWN_LICENCES!r} OR NOT redistribution"
)
QC_FLAGS = ("unchecked", "passed", "suspect", "failed")
WIGOS_ID = "^[0-9]{1,5}-[0-9]{1,5}-[0-9]{1,5}-[0-9A-Za-z]{1,16}$"


def _value_constraints() -> tuple[Any, ...]:
    return (
        CheckConstraint("value <> 'NaN'::numeric", name="value"),
        ForeignKeyConstraint(
            ["variable", "unit"], ["weather_variable.name", "weather_variable.unit"]
        ),
    )


class WeatherVariable(Base):
    __tablename__ = "weather_variable"
    __table_args__ = (UniqueConstraint("name", "unit"), UniqueConstraint("legacy_name"))

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    unit: Mapped[str] = mapped_column(Text)
    legacy_name: Mapped[str | None] = mapped_column(Text)


class WeatherReceipt(Base):
    __tablename__ = "weather_receipt"
    __table_args__ = (
        CheckConstraint("length(btrim(licence)) BETWEEN 1 AND 100", name="licence"),
        CheckConstraint(
            UNKNOWN_NOT_REDISTRIBUTED,
            name="unknown_not_redistributed",
        ),
        CheckConstraint(
            "payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash"
        ),
    )

    id: Mapped[int] = _pk()
    provider: Mapped[str] = mapped_column(Text)
    dataset: Mapped[str] = mapped_column(Text)
    farm_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("farm.id"))
    request: Mapped[dict[str, Any]] = _props()
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    licence: Mapped[str] = mapped_column(Text, server_default=UNKNOWN_LICENCE)
    attribution: Mapped[str | None] = mapped_column(Text)
    redistribution: Mapped[bool] = mapped_column(Boolean, server_default=false())
    payload_hash: Mapped[str | None] = mapped_column(Text)
    library_version: Mapped[str | None] = mapped_column(Text)


class WeatherStation(Base):
    __tablename__ = "weather_station"
    __table_args__ = (
        UniqueConstraint("provider", "provider_station_id"),
        UniqueConstraint("wigos_id"),
        CheckConstraint(f"wigos_id IS NULL OR wigos_id ~ '{WIGOS_ID}'", name="wigos_id"),
        Index("ix_weather_station_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[int] = _pk()
    provider: Mapped[str] = mapped_column(Text)
    provider_station_id: Mapped[str] = mapped_column(Text)
    wigos_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False))
    elevation_m: Mapped[Decimal | None] = mapped_column(Numeric)
    owner_farm_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("farm.id"))


class WeatherObservation(Base):
    __tablename__ = "weather_observation"
    __table_args__ = (
        *_value_constraints(),
        CheckConstraint("period_end >= period_start", name="period"),
        CheckConstraint(f"qc_flag IN {QC_FLAGS!r}", name="qc_flag"),
        UniqueConstraint(
            "station_id",
            "variable",
            "period_start",
            "period_end",
            "provisional",
            name="uq_weather_observation_station_period",
        ),
    )

    id: Mapped[int] = _pk()
    station_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("weather_station.id"))
    receipt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("weather_receipt.id"), index=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    qc_flag: Mapped[str] = mapped_column(Text, server_default="unchecked")
    variable: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Numeric)
    provisional: Mapped[bool] = mapped_column(Boolean, server_default=false())
    revision_note: Mapped[str | None] = mapped_column(Text)


class WeatherForecast(Base):
    __tablename__ = "weather_forecast"
    __table_args__ = (
        *_value_constraints(),
        CheckConstraint("member >= 0", name="member"),
        CheckConstraint(
            "(period_start IS NULL AND period_end IS NULL) OR period_end >= period_start",
            name="period",
        ),
        UniqueConstraint(
            "receipt_id",
            "model",
            "valid_time",
            "member",
            "variable",
            name="uq_weather_forecast_receipt_value",
        ),
    )

    id: Mapped[int] = _pk()
    receipt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("weather_receipt.id"))
    model: Mapped[str] = mapped_column(Text)
    run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    member: Mapped[int] = mapped_column(Integer, server_default="0")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    variable: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Numeric)
