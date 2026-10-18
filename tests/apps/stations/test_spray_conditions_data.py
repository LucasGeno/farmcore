"""Spray conditions, real St-Émilion station file (Scene B), replayed to Mon 8 Apr 2024 12:00 local.

The hour 12:00-13:00 reads 12.6 km/h (breezy), 13:00-20:00 blows 20-27 km/h, rain falls
20:00-24:00, and the hour from midnight is calm and dry with no rain in the 2 h after it."""

from __future__ import annotations

import html
import re
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.core.access import default_farm
from farmcore.core.models.canonical import Asset
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

pytestmark = pytest.mark.data

SUMMARY = "Breezy until 13:00, dry, light wind from midnight"


def _text(body: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(body)).split())


def test_scene_b_8_april_noon_spray_conditions(client, session, settings, seeded, step) -> None:
    seeded("B")
    step("2024-04-08 12:00")
    station = session.scalar(
        select(Asset).where(
            Asset.kind == "station", Asset.farm_id == default_farm(session, settings).id
        )
    )
    page = html.unescape(client.get(f"/stations/{station.id}").text)
    cells = re.findall(r'data-spray-cell="(\w+)"', page)
    assert cells[:2] == ["marginal", "no"]
    assert cells[8:12] == ["no"] * 4  # 20:00-24:00: rain
    assert cells[12] == "good"  # 00:00
    assert SUMMARY in _text(page)
    assert SUMMARY in _text(client.get("/stations/today").text)


@pytest.mark.spanish_data
def test_10_april_06_weather_flags_keep_rain_fact_without_competing_with_advisor(
    client,
    session,
    settings,
    seeded,
    step,
):
    from farmcore.apps.advisor.engine import AdvisorConfig
    from farmcore.apps.advisor.engine.window import spray_window
    from farmcore.apps.stations.conditions import spray_conditions
    from tests.apps.advisor.conftest import STATION_CSV, load_station_csv

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    series = load_station_csv(STATION_CSV)
    window = spray_window(series, AdvisorConfig.for_region_year("FRI12", 2024), now)
    flags = spray_conditions([asdict(r) for r in series], now, "Europe/Paris")
    assert window.open_now is True
    assert flags["hours"][0]["reason"] == "Rain 0.2 mm"
    assert flags["summary"] == "Rain until 07:00, then dry, light wind"
    seeded("B")
    step("2024-04-10 06:00")
    station = session.scalar(
        select(Asset).where(
            Asset.kind == "station", Asset.farm_id == default_farm(session, settings).id
        )
    )
    page = html.unescape(client.get(f"/stations/{station.id}").text)
    section = page.split('id="spray-conditions"', 1)[1].split("</section>", 1)[0]
    assert "Rain 0.2 mm" in section
    assert "No spraying" not in section and "to spray" not in section
    assert "whether and when to apply" in _text(section)
    assert "Conservative weather view" in _text(section)
