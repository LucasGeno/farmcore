"""The push endpoint as a weather provider: what its receipts say about the data.

Readings arrive (`routes`), they are not fetched, so discovery and fetches serve nothing. The
provider declares the on-farm tier, which every receipt keeps (`store.record_receipt`), so the
merge layer ranks these readings first. The data is the farm's own: redistribution follows the
owner, who shares it through grants like any other farm data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from farmcore.core.weather.contract import (
    AuthField,
    BBox,
    Capabilities,
    Fetched,
    Licence,
    Point,
    Station,
    Tier,
    TimeRange,
)
from farmcore.core.weather.variables import VARIABLES

NAME = "weather-push"
LICENCE = Licence(
    identifier="LicenseRef-farm-own-data",
    attribution="The farm's own sensor",
    redistribution=True,
)


class PushProvider:
    name = NAME

    def discover_stations(self, bbox: BBox) -> list[Station]:
        return []  # a device's station is made when it is registered (`devices`)

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        raise NotImplementedError("pushed readings are received, never fetched")

    def fetch_forecast(self, point: Point, models: Sequence[str], run: datetime | None) -> Fetched:
        raise NotImplementedError("on-farm sensors serve no forecast")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            observations=True,
            forecast=False,
            variables=frozenset(VARIABLES),
            tier=Tier.ON_FARM,
        )

    def licence(self) -> Licence:
        return LICENCE

    def auth_schema(self) -> tuple[AuthField, ...]:
        return ()


PROVIDER = PushProvider()
