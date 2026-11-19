"""Which weather sources a farm holds data from, as their receipts state them.

`sources(repo)` lists each distinct provider, dataset, licence, attribution and endpoint kind
among the farm's receipts that hold at least one observation or forecast value, and whether that
source may be redistributed. Weather views render attribution from it; grant views split it into
what they share and what the licence gate withheld (`shared` / `withheld`). It reads receipt
metadata only, never a value, so a grant view may say what it withheld and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, or_, select

from farmcore.core.access import ScopedRepository
from farmcore.core.models.weather import (
    UNKNOWN_LICENCE,
    WeatherForecast,
    WeatherObservation,
    WeatherReceipt,
)

SCOPE = "stations:read"
NOT_STATED = "its licence does not say the data may be redistributed"
NOT_ALLOWED = "its licence does not allow redistribution"


@dataclass(frozen=True)
class Source:
    provider: str
    dataset: str
    licence: str
    attribution: str | None
    endpoint_kind: str | None
    redistribution: bool

    @property
    def withheld_reason(self) -> str | None:
        if self.redistribution:
            return None
        return NOT_STATED if self.licence == UNKNOWN_LICENCE else NOT_ALLOWED

    def as_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "licence": self.licence,
            "attribution": self.attribution,
            "endpoint_kind": self.endpoint_kind,
        }


def sources(repo: ScopedRepository) -> list[Source]:
    """The farm's weather sources, ordered by provider, dataset, then licence."""
    endpoint = WeatherReceipt.request["endpoint_kind"].as_string()
    stmt = (
        select(
            WeatherReceipt.provider,
            WeatherReceipt.dataset,
            WeatherReceipt.licence,
            WeatherReceipt.attribution,
            endpoint,
            WeatherReceipt.redistribution,
        )
        .distinct()
        .where(
            WeatherReceipt.farm_id == repo.farm_id,
            or_(
                exists().where(WeatherObservation.receipt_id == WeatherReceipt.id),
                exists().where(WeatherForecast.receipt_id == WeatherReceipt.id),
            ),
        )
        .order_by(
            WeatherReceipt.provider,
            WeatherReceipt.dataset,
            WeatherReceipt.licence,
            WeatherReceipt.attribution,
            endpoint,
            WeatherReceipt.redistribution,
        )
    )
    return [Source(*row) for row in repo.execute(stmt, scope=SCOPE, resource="weather_receipt")]


def shared(rows: list[Source]) -> list[Source]:
    return [s for s in rows if s.redistribution]


def withheld(rows: list[Source]) -> list[Source]:
    return [s for s in rows if not s.redistribution]
