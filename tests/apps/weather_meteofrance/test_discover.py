"""The portal's station list feeds the farm's source choice: stations near the pin are ranked
from the catalogue's coordinates and altitude, and a far station is never offered."""

from __future__ import annotations

from decimal import Decimal

from farmcore.apps.weather_meteofrance.provider import provider
from farmcore.core.weather.contract import Point
from farmcore.core.weather.selection import select


def test_a_pin_near_saint_emilion_ranks_the_portal_catalogue_stations(credentials, portal) -> None:
    portal()

    chosen = select([provider(credentials)], Point(44.9, -0.2), "FR", Decimal(30))

    assert [(c.station.provider_station_id, c.station.elevation_m) for c in chosen.stations] == [
        ("33394002", Decimal(38)),  # St-Émilion, about 2 km
        ("33281001", Decimal(47)),  # Bordeaux-Mérignac, about 40 km; Paris is not offered
    ]
    assert "8 m higher" in chosen.stations[0].reason
