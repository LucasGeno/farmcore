"""`stations_weather(day)` on the seeded demo farm, replayed to 10 Apr 2024 06:00 (the ingest
stores the open Météo-France station CSV up to now + 73 h). Expected values come straight from
the CSV file, not from the database."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.stations import tools
from farmcore.apps.stations.connector import FILE, read_hours
from tests.apps.advisor import scenes
from tests.apps.chat.assistant.toolkit import CHAT, assistant_reads, ctx_at, keys, write_counts

pytestmark = pytest.mark.data

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

PARIS = ZoneInfo("Europe/Paris")
NOW = "2024-04-10 06:00"
# The facts the model chooser may send: the allowlist plus the day label, the partial-day
# marker and the error fact. Literal on purpose: widening it is a test change.
WEATHER_FACTS = {"rain_mm", "t_min", "t_max", "wind_max_kmh", "rh_mean", "replayed"}
WEATHER_FACTS |= {"day", "until", "error"}


@pytest.fixture
def replayed(seeded, step):
    seeded("B")
    step(NOW)


def from_csv(data_dir, start: datetime, end: datetime) -> dict:
    """The day's aggregates computed from the raw CSV rows: readings with start <= ts < end,
    rain (an hour-ending total) with start < ts <= end; a capped day passes end = the cap."""
    every = read_hours(data_dir / FILE)
    hours = [h for h in every if start <= h.ts < end]

    def vals(v: str, rows=hours) -> list[float]:
        return [h.values[v] for h in rows if h.values[v] is not None]

    rained = [h for h in every if start < h.ts <= end]
    return {
        "rain_mm": round(sum(vals("rr1_mm", rained)), 1),
        "t_min": round(min(vals("t_c")), 1),
        "t_max": round(max(vals("t_c")), 1),
        "wind_max_kmh": round(max(vals("ff_kmh")), 1),
        "rh_mean": round(sum(vals("rh_pct")) / len(vals("rh_pct")), 1),
    }


def day_bounds(d: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(d).replace(tzinfo=PARIS)
    return start, start + timedelta(days=1)


@pytest.mark.parametrize(("day", "date"), [("tomorrow", "2024-04-11"), ("today", "2024-04-10")])
def test_weather_for_a_day_matches_the_station_file(
    session, settings, replayed, data_dir, day, date
):
    ctx = ctx_at(session, settings, NOW)

    result = tools.weather(ctx, day=day)

    assert result.facts == {"day": date, **from_csv(data_dir, *day_bounds(date)), "replayed": True}
    assert keys(result.facts) <= WEATHER_FACTS
    assert len(result.text.splitlines()) <= 3


def test_a_local_date_and_the_72_hour_cap(session, settings, replayed, data_dir):
    ctx = ctx_at(session, settings, NOW)
    cap = ctx.now + timedelta(hours=72)  # 13 Apr 06:00

    partial = tools.weather(ctx, day="2024-04-13")
    beyond = tools.weather(ctx, day="2024-04-14")

    start, _ = day_bounds("2024-04-13")
    assert partial.facts == {
        "day": "2024-04-13",
        **from_csv(data_dir, start, cap),
        "replayed": True,
        "until": "06:00",
    }
    assert partial.text.startswith("Weather on 13 Apr until 06:00, station")
    assert beyond.facts == {"error": "beyond forecast", "day": "2024-04-14"}
    assert keys(partial.facts) | keys(beyond.facts) <= WEATHER_FACTS


def test_a_partial_day_in_french(session, settings, replayed):
    ctx = ctx_at(session, settings, NOW, lang="fr")

    result = tools.weather(ctx, day="2024-04-13")

    assert result.facts["until"] == "06:00"
    assert result.text.startswith("Météo le 13/04 jusqu'à 06:00, station")


def test_a_whole_day_has_no_until(session, settings, replayed):
    ctx = ctx_at(session, settings, NOW)

    assert "until" not in tools.weather(ctx, day="2024-04-12").facts


def test_an_unknown_day(session, settings, replayed):
    ctx = ctx_at(session, settings, NOW)

    result = tools.weather(ctx, day="someday")

    assert result.facts == {"error": "unknown day"}
    assert keys(result.facts) <= WEATHER_FACTS


def test_the_day_after_tomorrow(session, settings, replayed):
    ctx = ctx_at(session, settings, NOW, lang="fr")

    result = tools.weather(ctx, day="après-demain")

    assert result.facts["day"] == "2024-04-12"
    assert result.text.startswith("Météo après-demain (12/04)")


def test_french_day_words_and_text(session, settings, replayed):
    ctx = ctx_at(session, settings, NOW, lang="fr")

    result = tools.weather(ctx, day="demain")

    assert result.facts["day"] == "2024-04-11"
    assert result.text.startswith("Météo demain (11/04)")


def test_reads_only_asset_and_observation_and_writes_nothing(session, settings, replayed):
    before = write_counts(session)
    ctx = ctx_at(session, settings, NOW)

    tools.weather(ctx, day="tomorrow")

    reads = assistant_reads(session)
    assert reads
    assert {(actor, resource) for actor, resource, _ in reads} <= {
        (f"chat:{CHAT}", "asset"),
        (f"chat:{CHAT}", "observation"),
    }
    assert write_counts(session) == before


def test_declared_tool():
    [spec] = tools.TOOLS
    assert (spec.name, spec.scope, spec.params) == (
        "stations_weather",
        "stations:read",
        {"day": "STRING"},
    )
