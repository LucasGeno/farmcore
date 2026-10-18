"""Hero goldens on station 33394002, season 2024.

Expected values come from the data profile and regions-2024 research (independent of the engine):
R48 first reaches ≥ 10 mm (with T48 ≥ 10 °C) at 8 Apr 22Z; next-2 h rain after 10 Apr 04Z is
0.2 mm; rain from 9 May 06Z to 15 May 04Z is ≈ 43.6 mm.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig, ProductRef, Spray, evaluate
from tests.apps.advisor.engine.synth import utc

pytestmark = pytest.mark.data

PARIS = ZoneInfo("Europe/Paris")
CONFIG = AdvisorConfig.for_region_year("FRI12", 2024)
COPPER = ProductRef(name="copper", product_type="contact", targets=("downy",))


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=PARIS)


def test_scene_a_10_april_spray(station_series):
    advice = evaluate(station_series, [], CONFIG, _local(2024, 4, 10, 6))

    assert advice.decision == "spray"
    gate = advice.reason("season_gate")
    assert gate.threshold == {"season_gate": date(2024, 4, 5)}
    assert "assumed" in gate.note
    downy = advice.reason("downy_3_10")
    assert downy.verdict == "risk"
    assert downy.inputs["active_events"] == [utc(2024, 4, 8, 22)]
    assert advice.reason("protection_downy").verdict == "no cover"
    window = advice.reason("spray_window")
    assert window.verdict == "open"
    assert window.inputs["rain_next_mm"] == pytest.approx(0.2)
    assert window.threshold["window_rain_next_max_mm"] == 0.5
    assert advice.next_window[0] == utc(2024, 4, 10, 4)
    assert [p.name for p in advice.products_recommended] == ["copper"]
    assert "replay_forecast" in advice.flags


def test_scene_b_15_may_spray_washed_off_not_expired(station_series):
    spray = Spray(ts=_local(2024, 5, 9, 8).astimezone(UTC), products=(COPPER,))

    advice = evaluate(station_series, [spray], CONFIG, _local(2024, 5, 15, 6))

    assert advice.decision == "spray"
    assert "washed_off" in advice.flags
    assert "expired" not in advice.flags
    assert 43 <= advice.mm_since_spray <= 45
    assert advice.mm_since_spray == pytest.approx(43.6)
    assert advice.reason("downy_3_10").verdict == "risk"
    assert advice.reason("protection_downy").verdict == "washed off"
