"""The headline's rain and the card's 3-10 row show the same number, the infection event's.

The headline once said "after 10.3 mm" (the 48 h ending at the event) while the `downy_3_10` row
showed `r48_mm` at now (5 mm, below the 10 mm threshold) next to the verdict "risk".
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.apps.advisor import service
from farmcore.apps.advisor.engine import Advice as EngineAdvice
from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs
from farmcore.apps.advisor.engine.types import RuleResult
from farmcore.core.models.canonical import Advice, Parcel
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)
EVENT = datetime(2024, 4, 8, 22, tzinfo=UTC)
HEADLINE_MM = re.compile(r"after (\d+\.\d) mm rain")


def _advice(inputs: dict) -> EngineAdvice:
    downy = RuleResult(rule="downy_3_10", inputs=inputs, threshold={}, verdict="risk", source="")
    return EngineAdvice(decision="wait", reasons=[downy])


def test_headline_reads_the_event_rain_from_the_reason():
    # The series holds 3 mm at the event; the reason says 10.27: the reason wins.
    series = [HourlyObs(ts=EVENT, rr1_mm=3.0, t_c=12.0, rh_pct=80.0, ff_kmh=5.0)]
    advice = _advice({"active_events": [EVENT], "event_r48_mm": 10.27})

    text = service.headline(advice, series, CONFIG, NOW, scenes.TZ)

    assert "infection risk after 10.3 mm rain" in text


def test_headline_guards_a_reason_without_event_values_with_the_series():
    series = [HourlyObs(ts=EVENT, rr1_mm=3.0, t_c=12.0, rh_pct=80.0, ff_kmh=5.0)]
    advice = _advice({"active_events": [EVENT]})

    text = service.headline(advice, series, CONFIG, NOW, scenes.TZ)

    assert "infection risk after 3.0 mm rain" in text


def _latest(session, parcel_id: int) -> Advice:
    return session.scalar(
        select(Advice)
        .where(Advice.parcel_id == parcel_id)
        .order_by(Advice.evaluated_at.desc(), Advice.id.desc())
        .limit(1)
    )


def _downy_row(page: str) -> str:
    return page.split('data-rule="downy_3_10"', 1)[1].split("</tr>", 1)[0]


@pytest.mark.data
def test_scene_b_headline_and_card_row_show_the_event_rain(client, session, seeded, step) -> None:
    seeded("B")
    step("2024-04-10 06:00")

    sprays = 0
    for parcel in session.scalars(select(Parcel).order_by(Parcel.id)):
        row = _latest(session, parcel.id)
        if row is None or row.decision != "spray":
            continue
        sprays += 1
        downy = next(r for r in row.reasons if r["rule"] == "downy_3_10")
        event_mm = downy["inputs"]["event_r48_mm"]
        assert downy["inputs"]["event_t48_c"] >= CONFIG.downy_temp_min_c.value
        assert event_mm >= CONFIG.downy_rain_min_mm.value

        match = HEADLINE_MM.search(row.props["headline"])
        assert match, row.props["headline"]
        assert float(match.group(1)) == round(event_mm, 1)

        # Raw input keys render in debug mode only; the farmer checklist is in
        # test_card_plain.py. No admin token in tests: the client is admin.
        url = f"/advisor/parcels/{parcel.id}"
        page = html.unescape(client.get(url, params={"debug": "1"}).text)
        cells = _downy_row(page)
        shown = re.search(r"event_r48_mm</span> ([\d.]+)<", cells)
        assert shown, cells
        assert round(float(shown.group(1)), 1) == float(match.group(1))
        # Stored JSONB re-sorts keys; the card still lists the event values first.
        assert cells.index("event_r48_mm</span>") < cells.index(">r48_mm</span>")
    assert sprays > 0
