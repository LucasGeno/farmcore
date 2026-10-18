"""Package-edge composition: the one place that knows both an optional app and its consumer.

`compose_spray_weather_reader` picks the saved-spray-weather station reader from the
DISCOVERED apps (the installed software), not the apps active at startup: whether Weather is on
is read live inside the writing transaction (`operations.spray_weather`), so a Weather app
switched on after start can still be captured. No database work here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from farmcore.core.manifest import Manifest

WeatherReader = Callable[[Any, datetime], dict[str, Any]]

STATIONS = "stations"


def compose_spray_weather_reader(available: Iterable[Manifest]) -> WeatherReader | None:
    """The stations exact-slot reader when the stations app is discovered, else None."""
    if not any(m.name == STATIONS for m in available):
        return None
    from farmcore.apps.stations.capture import read_slot  # advertised: an import error is a bug

    return read_slot
