"""Which windows a run asks for: only whole days the farm lacks, each gap on its own, so a
farm with most of its season never re-orders it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from farmcore.apps.weather_era5.ingest import plan
from farmcore.core.weather.contract import TimeRange
from tests.apps.weather_era5.conftest import utc

H = timedelta(hours=1)
FIRST = utc(2026, 1, 1)  # the season starts
LAST = utc(2026, 1, 10)  # the last day ERA5 serves yet
NOW = utc(2026, 1, 20, 10, 37)


def _days(*days: int) -> set:
    """Every hour of January `days` (a valid time 00:00 to 23:00)."""
    return {utc(2026, 1, d) + i * H for d in days for i in range(24)}


@pytest.mark.parametrize(
    ("held", "windows"),
    [
        # A new farm: the whole season, one window.
        (set(), [TimeRange(FIRST - H, utc(2026, 1, 10, 23))]),
        # Days 1 to 8 held: only 9 and 10.
        (_days(*range(1, 9)), [TimeRange(utc(2026, 1, 8, 23), utc(2026, 1, 10, 23))]),
        # A hole in the middle (days 4 and 5, and day 7 short of one hour) is two windows.
        (
            _days(1, 2, 3, 6, 8, 9, 10) | {utc(2026, 1, 7) + i * H for i in range(23)},
            [
                TimeRange(utc(2026, 1, 3, 23), utc(2026, 1, 5, 23)),
                TimeRange(utc(2026, 1, 6, 23), utc(2026, 1, 7, 23)),
            ],
        ),
        # Everything held: nothing to ask.
        (_days(*range(1, 11)), []),
    ],
    ids=["new-farm", "tail-missing", "holes", "complete"],
)
def test_a_run_asks_only_for_the_days_the_farm_lacks(held, windows) -> None:
    assert plan(held, NOW, FIRST, max_days=92) == windows


def test_a_long_gap_is_split_so_one_request_stays_small() -> None:
    windows = plan(set(), NOW, FIRST, max_days=4)

    assert windows == [
        TimeRange(FIRST - H, utc(2026, 1, 4, 23)),
        TimeRange(utc(2026, 1, 4, 23), utc(2026, 1, 8, 23)),
        TimeRange(utc(2026, 1, 8, 23), utc(2026, 1, 10, 23)),
    ]
