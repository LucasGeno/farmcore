"""The merge layer: one flagged hourly series per farm from every weather source it holds.

Failure model (one test each):
- Priority: a value is taken from a lower tier (or a further station in the same tier) while a
  better-ranked source holds that hour and variable.
- Invented hours: an hour or a value appears that no source holds (interpolated, zero-filled).
- Determinism: the merged series depends on the order sources arrive in.
- Ranking input: the tier is read from a receipt's `endpoint_kind` instead of the provider's
  declaration (or, before providers declared one, the provider name), two providers serving one
  WIGOS station count as two sources or fail to store, a value is labelled with another tier
  the station also served, or the farm's stored automatic pick (`props["weather_sources"]`)
  decides instead of a recomputed ranking.
- Flag ranges: two filled hours around a missing one are reported as one range.
- As of: a replay of an earlier time reads a value whose receipt came later.
- Consumer: the advisor reads a gap-filled or forecast hour without its source per hour and
  variable in its inputs, card and JSON; reads the latest forecast fetched after now; ranks
  every parcel from one farm point; or skips a farm whose weather has no station asset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import delete, select, update

from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.weather import WeatherObservation
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather import merge
from farmcore.core.weather.contract import (
    BBox,
    Capabilities,
    Fetched,
    ForecastValue,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    Tier,
    TimeRange,
)
from farmcore.core.weather.store import receive
from farmcore.core.weather.variables import BY_LEGACY

T0 = datetime(2026, 6, 1, tzinfo=UTC)
FIELDS = merge.FIELDS

# --- pure merge: properties ------------------------------------------------------------------

_value = st.one_of(st.none(), st.floats(-50, 50, allow_nan=False).map(lambda v: round(v, 1)))
_hours = st.dictionaries(
    st.integers(0, 12).map(lambda h: T0 + timedelta(hours=h)),
    st.fixed_dictionaries({f: _value for f in FIELDS}).map(
        lambda row: {k: v for k, v in row.items() if v is not None}
    ),
    max_size=8,
)
_sources = st.lists(
    st.builds(
        lambda i, tier, distance, hours: merge.Source(f"s{i}", tier, distance, hours),
        st.integers(0, 50),
        st.sampled_from([*Tier, None]),
        st.one_of(st.none(), st.floats(0, 60_000, allow_nan=False)),
        _hours,
    ),
    max_size=5,
    unique_by=lambda s: s.identity,
)


ORDER = [Tier.ON_FARM, Tier.NETWORK, Tier.NATIONAL, Tier.MODEL, None]  # spec W4, best first


def _expected_rank(source: merge.Source) -> tuple:
    """Spec W4 order, written independently of `merge.rank_key`: tier, then the nearer station
    (an unknown distance last), then the identity."""
    distance = source.distance_m
    return (ORDER.index(source.tier), distance is None, distance or 0.0, source.identity)


@given(_sources)
def test_each_value_comes_from_the_best_ranked_source_holding_it(sources):
    ranked = sorted(sources, key=_expected_rank)
    for hour in merge.merge(sources).hours:
        for name in FIELDS:
            holders = [s for s in ranked if s.hours.get(hour.ts, {}).get(name) is not None]
            if holders:
                assert hour.filled_by[name] == holders[0].identity
                assert hour.values[name] == holders[0].hours[hour.ts][name]
            else:
                assert name not in hour.filled_by and hour.values[name] is None


@given(_sources)
def test_no_hour_or_value_is_invented(sources):
    held = {ts for s in sources for ts, row in s.hours.items() if row}
    merged = merge.merge(sources)
    assert [h.ts for h in merged.hours] == sorted(held)
    by_id = {s.identity: s for s in sources}
    for hour in merged.hours:
        for name, value in hour.values.items():
            if value is None:
                continue
            assert by_id[hour.filled_by[name]].hours[hour.ts][name] == value


@given(_sources, st.randoms(use_true_random=False))
def test_the_merge_does_not_depend_on_the_order_sources_arrive_in(sources, rnd):
    shuffled = list(sources)
    rnd.shuffle(shuffled)
    first, second = merge.merge(sources), merge.merge(shuffled)
    assert first.hours == second.hours
    assert first.sources == second.sources
    assert first.gap_filled() == second.gap_filled()


# --- stored sources: ranking input -----------------------------------------------------------

PIN = Point(44.9, -0.2)
OPEN = Licence("CC-BY-4.0", "© Test", True)


class Fake:
    """A provider declaring `tier`; its responses name `endpoint_kind`."""

    def __init__(self, name: str, tier: Tier | None) -> None:
        self.name, self._tier = name, tier

    def discover_stations(self, bbox: BBox) -> list[Station]:
        return []

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        raise NotImplementedError

    def fetch_forecast(self, point, models, run) -> Fetched:
        raise NotImplementedError

    def capabilities(self) -> Capabilities:
        return Capabilities(True, False, frozenset(), tier=self._tier)

    def licence(self) -> Licence:
        return OPEN

    def auth_schema(self):
        return ()


def _fetched(
    rain: dict[int, str], endpoint_kind: str, payload: bytes, fetched_at: datetime = T0
) -> Fetched:
    variable = BY_LEGACY["rr1_mm"]
    values = [
        ObservationValue(
            variable.name,
            variable.unit,
            Decimal(v),
            T0 + timedelta(hours=h - 1),
            T0 + timedelta(hours=h),
        )
        for h, v in rain.items()
    ]
    return Fetched(Request("hourly", None, {}, endpoint_kind), fetched_at, payload, values)


WIGOS = "0-20000-0-07510"


def test_tier_comes_with_each_value_and_one_wigos_station_is_one_source(session, settings):
    farm = ensure_default_farm(session, settings)
    nat = Station("NAT1", "National station 30 km away", 45.17, -0.2, wigos_id=WIGOS)
    # The national station misses hours 2, 5 to 8 and calls its endpoint "model-reanalysis";
    receive(
        session,
        Fake("national-service", Tier.NATIONAL),
        _fetched({1: "1.0", 3: "3.0"}, "model-reanalysis", b"n"),
        station=nat,
        farm_id=farm.id,
    )
    # the live portal serves the same WIGOS station under its own ID (hour 4), declaring no
    # tier: its provider name reads national, so it is the same source;
    portal = Station("PORTAL-7510", "Same station, live", 45.17, -0.2, wigos_id=WIGOS)
    receive(
        session,
        Fake("meteofrance-portal", None),
        _fetched({4: "4.0"}, "public", b"p"),
        station=portal,
        farm_id=farm.id,
    )
    # a model served at that station's WIGOS (hour 5) stays model tier;
    receive(
        session,
        Fake("station-model", Tier.MODEL),
        _fetched({5: "5.0"}, "public", b"sm"),
        station=Station("SM-7510", "Model at the station", 45.17, -0.2, wigos_id=WIGOS),
        farm_id=farm.id,
    )
    # the nearer model grid point lacks hours 5 and 7, calls itself "public", and the stored
    # automatic pick names it;
    receive(
        session,
        Fake("some-model", Tier.MODEL),
        _fetched({1: "9.0", 2: "2.0", 3: "9.0", 4: "9.0", 6: "6.0", 8: "8.0"}, "public", b"m"),
        station=Station("GRID1", "Model grid point next to the pin", 44.9, -0.2),
        farm_id=farm.id,
    )
    # the national station's hour 2 arrives late (a backfill fetched at hour 12).
    receive(
        session,
        Fake("national-service", Tier.NATIONAL),
        _fetched({2: "2.5"}, "public", b"late", fetched_at=T0 + timedelta(hours=12)),
        station=nat,
        farm_id=farm.id,
    )
    farm.props = {
        **(farm.props or {}),
        "weather_sources": {"stations": [{"provider": "some-model", "id": "GRID1"}]},
    }
    session.flush()
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "merge")
    end = T0 + timedelta(hours=8)

    merged = merge.farm_hourly(repo, T0, end, PIN, as_of=end)

    assert [
        (h.ts.hour, h.values["rr1_mm"], h.filled_by["rr1_mm"], h.tiers["rr1_mm"])
        for h in merged.hours
    ] == [
        (1, 1.0, WIGOS, Tier.NATIONAL),
        (2, 2.0, "some-model:GRID1", Tier.MODEL),
        (3, 3.0, WIGOS, Tier.NATIONAL),
        (4, 4.0, WIGOS, Tier.NATIONAL),
        (5, 5.0, WIGOS, Tier.MODEL),
        (6, 6.0, "some-model:GRID1", Tier.MODEL),
        (8, 8.0, "some-model:GRID1", Tier.MODEL),
    ]
    assert merge.sources_of(merged) == [
        {"source": WIGOS, "tier": "national"},
        {"source": "some-model:GRID1", "tier": "model"},
        {"source": WIGOS, "tier": "model"},
    ]
    at = [(T0 + timedelta(hours=h)).isoformat() for h in range(9)]
    assert merged.gap_filled() == [
        {
            "source": "some-model:GRID1",
            "tier": "model",
            "variable": "rr1_mm",
            "hours": [[at[2], at[2]], [at[6], at[6]], [at[8], at[8]]],
        },
        {"source": WIGOS, "tier": "model", "variable": "rr1_mm", "hours": [[at[5], at[5]]]},
    ]
    # Read after the late receipt, hour 2 is the national station's.
    later = merge.farm_hourly(repo, T0, end, PIN, as_of=T0 + timedelta(hours=12))
    assert (later.hours[1].values["rr1_mm"], later.hours[1].filled_by["rr1_mm"]) == (2.5, WIGOS)


# --- consumer: the advisor -------------------------------------------------------------------


def test_the_advisor_reads_and_flags_gap_filled_and_forecast_hours_per_hour(
    client, session, settings
):
    from farmcore.apps.advisor.advice import Advice
    from farmcore.apps.advisor.job import evaluate
    from farmcore.core.manifest import JobContext
    from farmcore.core.models.canonical import Parcel
    from tests.apps.advisor.review_fixtures import complete_station
    from tests.apps.chat.test_handlers import seed_farm

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    gap, later = now - timedelta(hours=5), now + timedelta(hours=2)
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    station = complete_station(session, parcel.farm_id, now)
    session.execute(
        delete(WeatherObservation).where(
            WeatherObservation.period_end == gap,
            WeatherObservation.variable == BY_LEGACY["rr1_mm"].name,
        )
    )
    receive(
        session,
        Fake("reanalysis", Tier.MODEL),
        Fetched(
            Request("hourly", None, {}, "model-reanalysis"),
            now,
            b"r",
            [
                ObservationValue(
                    BY_LEGACY["rr1_mm"].name, "mm", Decimal("0.4"), gap - timedelta(hours=1), gap
                ),
                # an observed future hour (replay surrogate): the forecast never replaces it
                ObservationValue(
                    BY_LEGACY["rr1_mm"].name,
                    "mm",
                    Decimal("0.1"),
                    later - timedelta(hours=1),
                    later,
                ),
            ],
        ),
        station=Station("CELL", "Grid cell", 44.9, -0.2),
        farm_id=parcel.farm_id,
    )
    ahead = now + timedelta(hours=1)  # no observation: the forecast fills it

    def forecast(fetched_at, payload, values):
        receive(
            session,
            Fake("forecaster", Tier.MODEL),
            Fetched(
                Request("forecast"),
                fetched_at,
                payload,
                [
                    ForecastValue("m", None, ts, BY_LEGACY[n].name, BY_LEGACY[n].unit, Decimal(v))
                    for ts, n, v in values
                ],
            ),
            farm_id=parcel.farm_id,
        )

    # At `later` rain is observed: the forecast fills only its temperature.
    forecast(
        now - timedelta(hours=1),
        b"f",
        [(gap, "rr1_mm", "9.9"), (ahead, "rr1_mm", "0.6"), (later, "rr1_mm", "7.0")]
        + [(later, "t_c", "20.0")],
    )
    forecast(now + timedelta(hours=1), b"later", [(ahead, "rr1_mm", "5.0")])  # not out yet

    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))

    row = session.scalars(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    ).first()
    assert "weather_gap_filled" in row.props["flags"]
    saved = client.get(f"/advisor/parcels/{parcel.id}/assessments/{row.id}.json").json()
    snapshot = saved["input_snapshot"]
    filled = [
        {
            "source": "reanalysis:CELL",
            "tier": "model",
            "variable": "rr1_mm",
            "hours": [[gap.isoformat(), gap.isoformat()], [later.isoformat(), later.isoformat()]],
        },
        {
            "source": "forecaster:forecast",
            "tier": "model",
            "variable": "rr1_mm",
            "hours": [[ahead.isoformat(), ahead.isoformat()]],
        },
        {
            "source": "forecaster:forecast",
            "tier": "model",
            "variable": "t_c",
            "hours": [[later.isoformat(), later.isoformat()]],
        },
    ]
    assert snapshot["weather_gap_filled"] == filled
    assert [(s["source"], s["tier"]) for s in snapshot["weather_sources"][1:]] == [
        ("reanalysis:CELL", "model"),
        ("forecaster:forecast", "model"),
    ]
    assert snapshot["weather_sources"][0]["tier"] == "on-farm"
    # Every hour names its source per variable (an index into weather_sources).
    own = {"rr1_mm": 0, "t_c": 0, "rh_pct": 0, "ff_kmh": 0}
    rows = snapshot["hourly_inputs"]
    assert rows[0]["filled_by"] == own
    assert [
        (r["ts"], r["rr1_mm"], r["t_c"], r["filled_by"]) for r in rows if r["filled_by"] != own
    ] == [
        (gap.isoformat(), 0.4, 15.0, {**own, "rr1_mm": 1}),
        (ahead.isoformat(), 0.6, None, {"rr1_mm": 2}),
        (later.isoformat(), 0.1, 20.0, {"rr1_mm": 1, "t_c": 2}),
    ]
    parcel_json = client.get(f"/advisor/parcels/{parcel.id}.json").json()
    assert parcel_json["weather_filled"][-3:] == filled  # after the on-farm station's runs
    page = client.get(f"/advisor/parcels/{parcel.id}").text
    assert 'data-flag="weather_gap_filled"' in page
    assert "reanalysis:CELL" in page

    # The station asset's own station, given a WIGOS ID, gets a model value from another
    # provider; where that is the only value, it is recorded with its source (only the asset's
    # own provider and tier keep the snapshot's compatibility shape).
    from farmcore.apps.advisor import service
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.weather import WeatherStation

    session.execute(
        update(WeatherStation).where(WeatherStation.provider == "synthetic").values(wigos_id=WIGOS)
    )
    alone = now + timedelta(days=30)
    receive(
        session,
        Fake("grid-model", Tier.MODEL),
        Fetched(
            Request("hourly"),
            alone,
            b"gm",
            [
                ObservationValue(
                    BY_LEGACY["rr1_mm"].name,
                    "mm",
                    Decimal("0.2"),
                    alone - timedelta(hours=1),
                    alone,
                )
            ],
        ),
        station=Station("GM", "Model at the station", 44.9, -0.2, wigos_id=WIGOS),
        farm_id=parcel.farm_id,
    )
    farm = session.get(Farm, parcel.farm_id)
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "merge")
    own = service.station_identities(repo, station)

    def only(at):
        return merge.load(repo, at - timedelta(hours=1), at, alone).at(None)

    assert [(s.identity, s.tier) for s in only(alone).sources] == [(WIGOS, Tier.MODEL)]
    assert service.records_sources(only(alone), own) is True
    assert service.records_sources(only(gap - timedelta(hours=2)), own) is False


def test_each_parcel_reads_its_nearest_station_without_a_station_asset(session, settings):
    from farmcore.apps.advisor.advice import Advice
    from farmcore.apps.advisor.job import evaluate
    from farmcore.core.farm_location import LOCATION_KEY
    from farmcore.core.manifest import JobContext
    from farmcore.core.models.canonical import Farm, Parcel
    from tests.apps.chat.test_handlers import seed_farm

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    ids = seed_farm(session, settings)

    def square(lon):
        return (
            f"SRID=4326;MULTIPOLYGON((({lon} 44.9,{lon + 0.001} 44.9,{lon + 0.001} 44.901,"
            f"{lon} 44.901,{lon} 44.9)))"
        )

    session.get(Parcel, ids["P1"]).geom = square(-0.2)
    session.get(Parcel, ids["P6"]).geom = square(-0.6)
    session.get(Parcel, ids["P5"]).geom = "SRID=4326;MULTIPOLYGON EMPTY"  # no centroid: the pin
    farm = session.get(Farm, session.get(Parcel, ids["P1"]).farm_id)
    farm.props = {**(farm.props or {}), LOCATION_KEY: {"latitude": 44.9, "longitude": -0.6}}
    network = Fake("network", Tier.NETWORK)
    for station, rain in (("EAST", "1.0"), ("WEST", "2.0")):
        lon = -0.2 if station == "EAST" else -0.6
        receive(
            session,
            network,
            _fetched_at(now, rain, station.encode()),
            station=Station(station, station, 44.9, lon),
            farm_id=farm.id,
        )

    evaluate(JobContext(session, now, settings, (farm.id,)))

    rain, sources = {}, {}
    for alias in ("P1", "P5", "P6"):
        row = session.scalars(
            select(Advice).where(Advice.parcel_id == ids[alias]).order_by(Advice.id.desc())
        ).first()
        snapshot = saved_evidence(session, farm, row)["input_snapshot"]
        rain[alias] = snapshot["hourly_inputs"][-1]["rr1_mm"]
        sources[alias] = (
            snapshot["weather_sources"],
            snapshot["hourly_inputs"][-1]["filled_by"],
            row.props["weather_filled"][0]["source"],
        )
    assert rain == {"P1": 1.0, "P5": 2.0, "P6": 2.0}
    # One source, no station asset: the source is still saved per hour and in the advice.
    assert sources["P1"] == (
        [{"source": "network:EAST", "tier": "network"}],
        {"rr1_mm": 0},
        "network:EAST",
    )


def _fetched_at(now: datetime, rain: str, payload: bytes) -> Fetched:
    variable = BY_LEGACY["rr1_mm"]
    value = ObservationValue(
        variable.name, variable.unit, Decimal(rain), now - timedelta(hours=1), now
    )
    return Fetched(Request("hourly"), now, payload, [value])


def saved_evidence(session, farm, row):
    from farmcore.apps.advisor import service

    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "merge")
    return service.saved_evidence(repo, row)
