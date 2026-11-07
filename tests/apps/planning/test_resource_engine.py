"""Resource-aware eligibility: weather, duration, capacity and configured availability combine,
and an unknown input is never reported as safe."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.planning import engine
from tests.apps.advisor.engine.synth import rain, series, utc

HOUR = timedelta(hours=1)
TZ = "Europe/Paris"
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
NOW = utc(2024, 4, 9, 4)  # 06:00 Paris
END = utc(2024, 4, 10, 20)


def _span(start_hour: int, minutes: int) -> engine.Span:
    start = utc(2024, 4, 9, start_hour)
    return engine.Span(start, start + timedelta(minutes=minutes))


def test_eligible_hours_combine_weather_duration_and_resource_windows():
    # Rain from 09:00 UTC closes 07:00 and 08:00 (the two-hour lookahead), leaving 06:00 open.
    weather_series = series(NOW, END, overrides=rain(utc(2024, 4, 9, 9), 10.0, 1))
    span = _span(6, 120)

    weather = engine.weather_state(weather_series, span, CONFIG)
    assert weather.status == "closed"
    assert "rain" in weather.reason

    machine = engine.availability(
        [engine.Window(utc(2024, 4, 9, 5), utc(2024, 4, 9, 10), True)], span
    )
    operator = engine.availability(
        [engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 10), True)], span
    )
    capacity = engine.capacity_state(Decimal("2.0"), area_ha=Decimal("1.0"), duration_minutes=120)
    assert capacity.status == "fits"

    assessment = engine.assess(weather, machine, operator, capacity)
    assert assessment.status == "infeasible"
    assert "rain" in " ".join(assessment.reasons)

    # The same resources leave only 06:00-07:00 open before the rain: a 90-minute job cannot fit.
    open_span = _span(6, 60)
    open_weather = engine.weather_state(
        series(NOW, END, overrides=rain(utc(2024, 4, 9, 9), 10.0, 1)), open_span, CONFIG
    )
    short = engine.assess(
        open_weather,
        engine.availability(
            [engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 8), True)], open_span
        ),
        engine.availability(
            [engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 7), True)], open_span
        ),
        engine.capacity_state(Decimal("2.0"), area_ha=Decimal("2.0"), duration_minutes=60),
    )
    # Capacity needs 60 minutes but the operator window closes after 60: it fits exactly here.
    assert short.status == "feasible"

    tighter = engine.assess(
        engine.weather_state(series(NOW, END), _span(6, 90), CONFIG),
        engine.availability(
            [engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 9), True)], _span(6, 90)
        ),
        engine.availability(
            [engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 7), True)], _span(6, 90)
        ),
        engine.capacity_state(Decimal("2.0"), area_ha=Decimal("2.0"), duration_minutes=90),
    )
    assert tighter.status == "infeasible"
    assert any("availability" in reason for reason in tighter.reasons)


def test_unknown_capacity_or_availability_is_unknown_never_safe():
    weather = engine.weather_state(series(NOW, END), _span(6, 90), CONFIG)
    machine = engine.availability([], _span(6, 90))
    operator = engine.availability(
        [engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 8), True)], _span(6, 90)
    )
    capacity = engine.capacity_state(None, area_ha=Decimal("1.25"), duration_minutes=90)

    assert weather.status == "open"
    assert machine.status == "unknown"
    assert "no configured availability" in machine.reason
    assert capacity.status == "unknown"
    assert "capacity" in capacity.reason.lower()

    assessment = engine.assess(weather, machine, operator, capacity)
    assert assessment.status == "unknown"
    assert assessment.status != "feasible"
    assert "Machine" in " ".join(assessment.reasons)


def test_capacity_needs_more_time_than_the_job_offers():
    too_short = engine.capacity_state(Decimal("1.0"), area_ha=Decimal("2.0"), duration_minutes=60)
    assert too_short.status == "too_short"
    assert "120" in too_short.reason

    fits = engine.capacity_state(Decimal("4.0"), area_ha=Decimal("2.0"), duration_minutes=60)
    assert fits.status == "fits"


def test_alternate_start_respects_weather_windows_bookings_and_busy_days():
    weather_series = series(NOW, END)
    operator_window = engine.Window(utc(2024, 4, 9, 6), utc(2024, 4, 9, 12), True)

    offered = engine.next_feasible_span(
        series=weather_series,
        now=NOW,
        tz=TZ,
        config=CONFIG,
        earliest=utc(2024, 4, 9, 6),
        duration_minutes=60,
        horizon_hours=12,
        operator_windows=[operator_window],
        machine_windows=None,
        operator_bookings=[engine.Span(utc(2024, 4, 9, 6), utc(2024, 4, 9, 9))],
        machine_bookings=None,
        work_rate=None,
        area_ha=Decimal("1.0"),
        busy_days=set(),
    )
    assert offered == engine.Span(utc(2024, 4, 9, 9), utc(2024, 4, 9, 10))

    busy = engine.next_feasible_span(
        series=weather_series,
        now=NOW,
        tz=TZ,
        config=CONFIG,
        earliest=utc(2024, 4, 9, 6),
        duration_minutes=60,
        horizon_hours=12,
        operator_windows=[operator_window],
        machine_windows=None,
        operator_bookings=[],
        machine_bookings=None,
        work_rate=None,
        area_ha=Decimal("1.0"),
        busy_days={date(2024, 4, 9)},
    )
    assert busy is None


def test_the_product_constraint_joins_the_weather_window_for_sulphur():
    hot = series(NOW, END, t=35.0)
    span = _span(6, 60)
    assert engine.weather_state(hot, span, CONFIG).status == "open"
    sulphur = engine.weather_state(hot, span, CONFIG, sulphur=True)
    assert sulphur.status == "closed"
    assert "sulphur" in sulphur.reason


def test_weather_state_says_unknown_when_forecast_hours_are_missing():
    # A short series leaves the requested evening span beyond its last hour.
    short_series = series(NOW, utc(2024, 4, 9, 9))
    weather = engine.weather_state(short_series, _span(14, 60), CONFIG)
    assert weather.status == "unknown"
    assert "forecast" in weather.reason.lower()
