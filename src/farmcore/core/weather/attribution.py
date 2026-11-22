"""Which weather sources a farm holds data from, as their receipts state them.

`sources(repo)` lists each distinct provider, dataset, licence, attribution and endpoint kind
among the farm's receipts, and whether that source may be redistributed. The owner's weather
views list the receipts that hold at least one observation or forecast value and say which are
not shared. A grant or token view (`repo.grant_view`) lists only receipts whose licence allows
redistribution, whether or not they hold values: withheld weather is indistinguishable from none
(owner ruling 2026-11-22), so no grant view probes a withheld receipt or its values, not even
for a yes/no. What it withheld is said generically from the grant's own scope
(`withheld_note`), the same for a farm with withheld weather and a farm with none.
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
WITHHELD_NOTE = (
    "Weather whose licence does not allow redistribution is never shared: "
    "some inputs may be withheld by the data owner."
)


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
    """The farm's weather sources, ordered by provider, dataset, then licence. A grant view gets
    only the redistributable ones (see the module docstring)."""
    endpoint = WeatherReceipt.request["endpoint_kind"].as_string()
    if repo.grant_view:
        held = WeatherReceipt.redistribution.is_(True)
    else:
        held = or_(
            exists().where(WeatherObservation.receipt_id == WeatherReceipt.id),
            exists().where(WeatherForecast.receipt_id == WeatherReceipt.id),
        )
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
        .where(WeatherReceipt.farm_id == repo.farm_id, held)
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


def withheld_note(repo: ScopedRepository) -> str | None:
    """What a grant view says about withheld weather: a fixed sentence whenever its grant reads
    weather (`stations:read`), never derived from the farm's data. None for the owner, whose
    views name each withheld source (`withheld`)."""
    if not repo.grant_view or SCOPE not in repo.grant.scopes:
        return None
    return WITHHELD_NOTE
