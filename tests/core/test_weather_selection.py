"""Choosing weather sources from the farm pin: country providers, stations ranked by great-circle
distance and elevation difference, the model forecast always, and ERA5 backfill when installed.

Expected distances come from the sphere of the WGS84 mean radius (IUGG R1, 6 371 008.8 m): one
degree of arc is 111 195 m, wherever it lies.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from farmcore.core.weather.contract import BBox, Capabilities, Licence, Point, Station
from farmcore.core.weather.selection import great_circle_m, rank, select

FARM = Point(44.0, 0.0)


def _station(sid: str, north_deg: float, elevation: int | None) -> Station:
    return Station(
        provider_station_id=sid,
        name=f"Station {sid}",
        latitude=FARM.latitude + north_deg,
        longitude=FARM.longitude,
        elevation_m=None if elevation is None else Decimal(elevation),
    )


@pytest.mark.parametrize(
    ("a", "b", "metres"),
    [
        ((44.0, 0.0), (45.0, 0.0), 111_195),  # one degree along a meridian
        ((0.0, 179.9), (0.0, -179.9), 22_239),  # across the antimeridian, not 359.8 degrees
        ((43.3, -1.8), (43.3, -1.7), 8_092),  # Hendaye/Irun: a border, no projected shortcut
    ],
)
def test_great_circle_distance_holds_across_borders_and_the_antimeridian(a, b, metres) -> None:
    assert great_circle_m(Point(*a), Point(*b)) == pytest.approx(metres, abs=2)


def test_ranking_is_deterministic_and_down_ranks_a_station_over_300_m_higher_or_lower() -> None:
    stations = [
        ("fr", _station("near-high", 0.05, 800)),  # 5.6 km, 400 m higher
        ("fr", _station("near-low", 0.07, 90)),  # 7.8 km, 310 m lower
        ("fr", _station("mid", 0.10, 680)),  # 11.1 km, 280 m higher: within the band
        ("fr", _station("far", 0.20, 410)),  # 22.2 km, 10 m higher
        ("fr", _station("unknown", 0.15, None)),  # 16.7 km, elevation not published
    ]
    shuffled = stations[:]
    random.Random(4).shuffle(shuffled)

    ranked = rank(stations, FARM, Decimal(400))

    assert [c.station.provider_station_id for c in rank(shuffled, FARM, Decimal(400))] == [
        c.station.provider_station_id for c in ranked
    ]
    assert [(c.station.provider_station_id, c.down_ranked) for c in ranked] == [
        ("mid", False),
        ("unknown", False),
        ("far", False),
        ("near-high", True),
        ("near-low", True),
    ]
    reasons = {c.station.provider_station_id: c.reason for c in ranked}
    assert "400 m higher" in reasons["near-high"] and "down-ranked" in reasons["near-high"]
    assert "310 m lower" in reasons["near-low"] and "down-ranked" in reasons["near-low"]
    assert "elevation unknown" in reasons["unknown"]
    assert ranked[0].distance_m == pytest.approx(11_119.5, abs=1)


def test_an_unknown_farm_elevation_is_said_and_never_invented() -> None:
    ranked = rank([("fr", _station("high", 0.05, 900))], FARM, None)

    assert not ranked[0].down_ranked
    assert ranked[0].elevation_difference_m is None
    assert "farm elevation unknown" in ranked[0].reason


@dataclass
class FakeProvider:
    name: str
    caps: Capabilities
    stations: list[Station] = field(default_factory=list)
    broken: bool = False
    asked: list[BBox] = field(default_factory=list)

    def discover_stations(self, bbox: BBox) -> list[Station]:
        if self.broken:
            raise RuntimeError("catalogue down")
        self.asked.append(bbox)
        return [
            s
            for s in self.stations
            if bbox.west <= s.longitude <= bbox.east and bbox.south <= s.latitude <= bbox.north
        ]

    def fetch_observations(self, station, range):
        raise NotImplementedError

    def fetch_forecast(self, point, models, run):
        raise NotImplementedError

    def capabilities(self) -> Capabilities:
        return self.caps

    def licence(self) -> Licence:
        return Licence("CC-BY-4.0", "x", True)

    def auth_schema(self) -> tuple:
        return ()


def _obs(*countries: str) -> Capabilities:
    return Capabilities(True, False, frozenset(), frozenset(countries))


def test_selection_takes_country_providers_always_the_forecast_and_backfill_when_installed() -> (
    None
):
    french = FakeProvider("fr-obs", _obs("FR"), [_station("33", 0.05, 40)])
    spanish = FakeProvider("es-obs", _obs("ES"), [_station("28", 0.01, 40)])
    broken = FakeProvider("fr-down", _obs("FR"), broken=True)
    forecast = FakeProvider("model", Capabilities(False, True, frozenset()))
    providers = [french, spanish, broken, forecast]

    chosen = select(providers, FARM, "FR", Decimal(50))

    assert [c.provider for c in chosen.stations] == ["fr-obs"]
    assert spanish.asked == []  # another country's provider is never asked
    assert [f.provider for f in chosen.forecast] == ["model"]
    assert chosen.backfill == ()
    assert any("fr-down" in note for note in chosen.notes)  # a failure is said, not fatal
    assert any("backfill" in note.lower() for note in chosen.notes)

    era5 = FakeProvider("era5", Capabilities(False, False, frozenset(), backfill=True))
    with_era5 = select([*providers, era5], FARM, "FR", Decimal(50))
    assert [b.provider for b in with_era5.backfill] == ["era5"]


@pytest.mark.parametrize(
    ("farm", "station"),
    [
        (Point(0.0, 179.9), Point(0.0, -179.9)),  # 22 km across the antimeridian
        (Point(89.9, 0.0), Point(89.9, 180.0)),  # 22 km across the North Pole
    ],
)
def test_a_station_just_across_the_antimeridian_or_a_pole_is_still_found(farm, station) -> None:
    across = Station("x", "Across", station.latitude, station.longitude, Decimal(10))
    provider = FakeProvider("obs", Capabilities(True, False, frozenset()), [across])

    chosen = select([provider], farm, None, None)

    assert [c.station.provider_station_id for c in chosen.stations] == ["x"]
