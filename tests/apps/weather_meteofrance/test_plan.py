"""Which windows a live run asks for: DPObs keeps only the last 24 hours, so any hour the farm
lacks before that comes from the DPClim archive, or it would never be filled."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.weather_meteofrance.ingest import plan
from farmcore.core.weather.contract import TimeRange

NOW = datetime(2026, 11, 19, 10, 37, tzinfo=UTC)
HOUR = datetime(2026, 11, 19, 10, tzinfo=UTC)
H = timedelta(hours=1)


def _held(newest: int, oldest: int = 8 * 24) -> set[datetime]:
    """Every hour ending from `oldest` to `newest` hours before HOUR."""
    return {HOUR - i * H for i in range(newest, oldest + 1)}


@pytest.mark.parametrize(
    ("held", "windows"),
    [
        # Up to date three hours ago: only the hours since.
        (_held(3), [TimeRange(HOUR - 3 * H, HOUR)]),
        # Down for 30 hours: hours 29 to 24 ago are past DPObs, so the archive fills them.
        (_held(30), [TimeRange(HOUR - 30 * H, HOUR - 24 * H), TimeRange(HOUR - 24 * H, HOUR)]),
        # A new farm: a week of archive, then the 24-hour window.
        (set(), [TimeRange(HOUR - 7 * 24 * H, HOUR - 24 * H), TimeRange(HOUR - 24 * H, HOUR)]),
    ],
    ids=["up-to-date", "outage-past-24h", "new-farm"],
)
def test_a_run_fetches_recent_hours_from_dpobs_and_older_gaps_from_the_archive(
    held, windows
) -> None:
    assert plan(held, NOW) == windows
