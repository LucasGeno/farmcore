"""`stations_weather` with missing station hours (review R1): a day with gaps is never shown as a
complete day. Rain becomes a lower bound ("at least"), extrema and means say how many readings
they come from, a variable with no reading is "not recorded" (never 0), and the coverage facts
count each variable on its own: rain on hour-ending slots (00:00, next 00:00], readings on
[00:00, next 00:00), both capped at now + 72 h. Synthetic station rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.stations import tools
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.seed import ensure_default_farm
from tests.apps.chat.assistant.toolkit import ctx_at, keys

PARIS = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")
NOW = "2024-04-10 06:00"
# The facts the model chooser may send. Literal on purpose: widening it is a test change.
WEATHER_FACTS = {"day", "rain_mm", "t_min", "t_max", "wind_max_kmh", "rh_mean", "replayed"}
WEATHER_FACTS |= {"until", "error", "expected_hours", "rain_hours", "t_hours", "wind_hours"}
WEATHER_FACTS |= {"rh_hours", "rain_until"}
VARIABLES = ("rr1_mm", "t_c", "ff_kmh", "rh_pct")


@pytest.fixture
def station(session, settings) -> Asset:
    farm = ensure_default_farm(session, settings)
    asset = Asset(farm_id=farm.id, kind="station", name="synthetic station")
    session.add(asset)
    session.flush()
    return asset


def add(session, station, rows) -> None:
    for ts, variable, value in rows:
        session.add(Observation(asset_id=station.id, ts=ts, variable=variable, value=value))
    session.commit()


def hours(day: str, n: int = 24, first: int = 0):
    start = datetime.fromisoformat(day).replace(tzinfo=PARIS).astimezone(UTC)
    return [start + timedelta(hours=h) for h in range(first, first + n)]


def full_day(day: str, rain: float = 0.5) -> list:
    """Every slot of a local day: rain at 01:00…24:00, readings at 00:00…23:00."""
    rows = [(ts + timedelta(hours=1), "rr1_mm", rain) for ts in hours(day)]
    for ts in hours(day):
        rows += [(ts, "t_c", 10.0), (ts, "ff_kmh", 5.0), (ts, "rh_pct", 70.0)]
    return rows


def weather(session, settings, day: str, lang: str = "en"):
    return tools.weather(ctx_at(session, settings, NOW, lang=lang), day=day)


def test_one_reading_is_not_a_full_dry_day(session, settings, station) -> None:
    noon = datetime(2024, 4, 11, 12, tzinfo=PARIS)
    add(
        session,
        station,
        [(noon, v, x) for v, x in zip(VARIABLES, (0.0, 12.0, 3.0, 80.0), strict=True)],
    )

    result = weather(session, settings, "tomorrow")

    assert result.facts == {
        "day": "2024-04-11",
        "rain_mm": 0.0,
        "t_min": 12.0,
        "t_max": 12.0,
        "wind_max_kmh": 3.0,
        "rh_mean": 80.0,
        "replayed": True,
        "expected_hours": 24,
        "rain_hours": 1,
        "t_hours": 1,
        "wind_hours": 1,
        "rh_hours": 1,
    }
    assert keys(result.facts) <= WEATHER_FACTS
    lines = result.text.splitlines()
    assert len(lines) == 3
    assert "at least 0 mm rain" in lines[0]
    assert lines[1] == (
        "Incomplete: rain 1 of 24 h, temperature 1 of 24, wind 1 of 24, humidity 1 of 24."
    )
    assert "Replayed Météo-France observations" in lines[2]


def test_a_complete_day_keeps_the_plain_answer(session, settings, station) -> None:
    add(session, station, full_day("2024-04-11"))

    result = weather(session, settings, "tomorrow")

    assert result.facts["rain_mm"] == 12.0
    assert "expected_hours" not in result.facts
    assert "at least" not in result.text and "Incomplete" not in result.text
    assert len(result.text.splitlines()) == 2


@pytest.mark.parametrize(
    ("drop", "rain_hours"),
    [
        ({1}, 23),  # the 00:00–01:00 rain, leading
        ({12, 13}, 22),  # interior
        ({24}, 23),  # the 23:00–24:00 rain, stamped at next midnight: trailing
    ],
)
def test_missing_rain_hours_make_the_total_a_lower_bound(
    session, settings, station, drop, rain_hours
) -> None:
    midnight = hours("2024-04-11")[0]
    gone = {midnight + timedelta(hours=k) for k in drop}  # rain is stamped at the hour's end
    rows = [r for r in full_day("2024-04-11", rain=1.0) if not (r[1] == "rr1_mm" and r[0] in gone)]
    add(session, station, rows)

    result = weather(session, settings, "tomorrow")

    assert result.facts["rain_hours"] == rain_hours
    assert result.facts["rain_mm"] == float(rain_hours)
    assert result.facts["t_hours"] == 24
    assert result.text.splitlines()[0].count(f"at least {rain_hours} mm rain") == 1
    assert f"rain {rain_hours} of 24 h" in result.text


def test_each_variable_is_counted_on_its_own(session, settings, station) -> None:
    add(session, station, [r for r in full_day("2024-04-11") if r[1] != "ff_kmh"])

    result = weather(session, settings, "tomorrow")

    assert result.facts["wind_max_kmh"] is None and result.facts["wind_hours"] == 0
    assert result.facts["rain_hours"] == 24 and result.facts["t_hours"] == 24
    assert "wind not recorded" in result.text
    assert "Incomplete: wind 0 of 24." in result.text


def test_no_reading_at_all_is_unknown_never_dry(session, settings, station) -> None:
    result = weather(session, settings, "tomorrow")

    assert result.facts["rain_mm"] is None and result.facts["rain_hours"] == 0
    assert "rain not recorded" in result.text and "0 mm" not in result.text


def test_the_cap_counts_only_the_hours_up_to_it(session, settings, station) -> None:
    # now 10 Apr 06:00 -> cap 13 Apr 06:00: rain slots 01:00..06:00 (6), readings 00:00..06:00 (7)
    add(session, station, full_day("2024-04-13"))

    result = weather(session, settings, "2024-04-13")

    assert result.facts["until"] == "06:00"
    assert "expected_hours" not in result.facts  # every slot up to the cap is there
    assert result.facts["rain_mm"] == 3.0


def test_a_dst_day_has_23_slots(session, settings, station) -> None:
    # 31 Mar 2024: Paris skips 02:00 -> 23 hours. now 30 Mar 06:00 keeps it inside the cap.
    day = "2024-03-31"
    start = datetime(2024, 3, 31, tzinfo=PARIS).astimezone(UTC)
    rows = [(start + timedelta(hours=h + 1), "rr1_mm", 1.0) for h in range(23)]
    rows += [(start + timedelta(hours=h), v, 1.0) for h in range(23) for v in VARIABLES[1:]]
    add(session, station, rows[:-3])  # drop the last three readings

    result = tools.weather(ctx_at(session, settings, "2024-03-30 06:00"), day=day)

    assert result.facts["expected_hours"] == 23
    assert result.facts["rain_hours"] == 23 and result.facts["rh_hours"] == 22


def test_french_incomplete_line(session, settings, station) -> None:
    noon = datetime(2024, 4, 11, 12, tzinfo=PARIS)
    add(session, station, [(noon, "t_c", 12.0)])

    lines = weather(session, settings, "demain", lang="fr").text.splitlines()

    assert "pluie non relevée" in lines[0]
    assert lines[1] == (
        "Incomplet : pluie 0 sur 24 h, température 1 sur 24, vent 0 sur 24, humidité 0 sur 24."
    )


@pytest.mark.parametrize(
    ("lang", "phrase"), [("en", "3 mm rain to 06:00"), ("fr", "3 mm de pluie jusqu'à 06:00")]
)
def test_a_cap_off_the_hour_names_the_last_complete_rain_hour(
    session, settings, station, lang, phrase
) -> None:
    # Re-review P3: now 06:30 -> cap 13 Apr 06:30. The hourly rain totals cover only up to
    # 06:00 (the 07:00 total would pass the cap): say so, never interpolate the last 30 min.
    add(session, station, full_day("2024-04-13"))
    ctx = ctx_at(session, settings, "2024-04-10 06:30", lang=lang)

    result = tools.weather(ctx, day="2024-04-13")

    assert result.facts["until"] == "06:30" and result.facts["rain_until"] == "06:00"
    assert result.facts["rain_mm"] == 3.0
    assert "expected_hours" not in result.facts
    assert keys(result.facts) <= WEATHER_FACTS
    assert phrase in result.text.splitlines()[0]


def test_a_cap_on_the_hour_needs_no_rain_cutoff(session, settings, station) -> None:
    add(session, station, full_day("2024-04-13"))

    result = tools.weather(ctx_at(session, settings, NOW), day="2024-04-13")

    assert "rain_until" not in result.facts
    assert "rain to" not in result.text
