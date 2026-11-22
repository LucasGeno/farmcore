"""The merge layer: one flagged hourly series per farm from every weather source it holds.

Failure model (one test each):
- Priority: a value is taken from a lower tier (or a further station in the same tier) while a
  better-ranked source holds that hour and variable.
- Invented hours: an hour or a value appears that no source holds (interpolated, zero-filled).
- Determinism: the merged series depends on the order sources arrive in.
- Ranking input: the tier is read from a receipt's `endpoint_kind` instead of the provider's
  declaration, two providers serving one station count as two sources, or the farm's stored
  automatic pick (`props["weather_sources"]`) decides instead of a recomputed ranking.
- Consumer: the advisor reads a gap-filled hour without flagging it in its inputs and card.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import delete, select

from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.weather import WeatherObservation
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather import merge
from farmcore.core.weather.contract import (
    BBox,
    Capabilities,
    Fetched,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    Tier,
    TimeRange,
)
from farmcore.core.weather.store import receive, record_receipt, store_observations, upsert_station
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


def _fetched(rain: dict[int, str], endpoint_kind: str, payload: bytes) -> Fetched:
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
    return Fetched(Request("hourly", None, {}, endpoint_kind), T0, payload, values)


def test_tier_comes_from_the_provider_and_one_station_is_one_source(session, settings):
    farm = ensure_default_farm(session, settings)
    national = Fake("national-service", Tier.NATIONAL)
    model = Fake("some-model", Tier.MODEL)
    far = Station("NAT1", "National station 30 km away", 45.17, -0.2)
    near = Station("GRID1", "Model grid point next to the pin", 44.9, -0.2)
    # The national station misses hour 2 and calls its endpoint "model-reanalysis";
    receive(
        session,
        national,
        _fetched({1: "1.0", 3: "3.0"}, "model-reanalysis", b"n"),
        station=far,
        farm_id=farm.id,
    )
    # a second provider serving the same station (live beside replay) writes that station's
    # row with hour 4, declaring no tier: still one source, at the best tier either declared;
    second = Fake("national-service-live", None)
    live = _fetched({4: "4.0"}, "public", b"live")
    receipt = record_receipt(
        session,
        provider=second.name,
        licence=OPEN,
        fetched=live,
        farm_id=farm.id,
        tier=second.capabilities().tier,
    )
    store_observations(session, receipt, upsert_station(session, national.name, far), live.values)
    # the model is nearer, calls itself "public", and the stored automatic pick names it.
    receive(
        session,
        model,
        _fetched({1: "9.0", 2: "2.0", 3: "9.0", 4: "9.0"}, "public", b"m"),
        station=near,
        farm_id=farm.id,
    )
    farm.props = {
        **(farm.props or {}),
        "weather_sources": {"stations": [{"provider": model.name, "id": "GRID1"}]},
    }
    session.flush()
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "merge")

    merged = merge.farm_hourly(repo, T0, T0 + timedelta(hours=4), PIN)

    assert [(h.ts.hour, h.values["rr1_mm"], h.filled_by["rr1_mm"]) for h in merged.hours] == [
        (1, 1.0, "national-service:NAT1"),
        (2, 2.0, "some-model:GRID1"),
        (3, 3.0, "national-service:NAT1"),
        (4, 4.0, "national-service:NAT1"),
    ]
    assert merge.sources_of(merged) == [
        {"source": "national-service:NAT1", "tier": "national"},
        {"source": "some-model:GRID1", "tier": "model"},
    ]
    assert merged.gap_filled() == [
        {
            "source": "some-model:GRID1",
            "tier": "model",
            "variable": "rr1_mm",
            "hours": [[(T0 + timedelta(hours=2)).isoformat()] * 2],
        }
    ]


# --- consumer: the advisor -------------------------------------------------------------------


def test_the_advisor_flags_a_gap_filled_hour_in_its_inputs_and_card(client, session, settings):
    from farmcore.apps.advisor.advice import Advice
    from farmcore.apps.advisor.job import evaluate
    from farmcore.core.manifest import JobContext
    from farmcore.core.models.canonical import Parcel
    from tests.apps.advisor.review_fixtures import complete_station
    from tests.apps.chat.test_handlers import seed_farm

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    gap = now - timedelta(hours=5)
    ids = seed_farm(session, settings)
    parcel = session.get(Parcel, ids["P1"])
    complete_station(session, parcel.farm_id, now)
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
                )
            ],
        ),
        station=Station("CELL", "Grid cell", 44.9, -0.2),
        farm_id=parcel.farm_id,
    )

    evaluate(JobContext(session, now, settings, (parcel.farm_id,)))

    row = session.scalars(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.id.desc())
    ).first()
    assert "weather_gap_filled" in row.props["flags"]
    saved = client.get(f"/advisor/parcels/{parcel.id}/assessments/{row.id}.json").json()
    snapshot = saved["input_snapshot"]
    assert snapshot["weather_gap_filled"] == [
        {
            "source": "reanalysis:CELL",
            "tier": "model",
            "variable": "rr1_mm",
            "hours": [[gap.isoformat(), gap.isoformat()]],
        }
    ]
    assert [s["tier"] for s in snapshot["weather_sources"]] == ["on-farm", "model"]
    page = client.get(f"/advisor/parcels/{parcel.id}").text
    assert 'data-flag="weather_gap_filled"' in page
    assert "reanalysis:CELL" in page
