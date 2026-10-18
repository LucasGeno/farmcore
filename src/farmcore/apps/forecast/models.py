"""Forecast tables (migration `forecast_0001`): one `forecast_run` per issued forecast, its hourly
values in `forecast_value` (the station's variable names, so the advisor rules read them as is).

`issued_at` is the run time (the model's start, as the source names the run); `available_at` is
when users could first read it (later: the run takes hours to compute and disseminate), never the
import time. A read at clock time `now` takes the latest run with `available_at <= now`
(`store.latest_run`). Both tables carry `farm_id`,
so the scoped repository filters them like any farm table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Double, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from farmcore.core.models.base import Base


class ForecastRun(Base):
    __tablename__ = "forecast_run"
    __table_args__ = (UniqueConstraint("farm_id", "source", "issued_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    source: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # label (plain words), as_issued (false: not the forecast as it was issued), model, grid
    # latitude/longitude, units per variable, the request URL, the attribution
    props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )


class ForecastValue(Base):
    __tablename__ = "forecast_value"
    __table_args__ = (UniqueConstraint("run_id", "ts", "variable"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    farm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("farm.id"))
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("forecast_run.id", ondelete="CASCADE")
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    variable: Mapped[str] = mapped_column(Text)
    value: Mapped[float] = mapped_column(Double)
