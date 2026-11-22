"""BBCH stage on a day: the grower's dated entries, else a dated window of phases. Stages only
move forward, so a later window does not undo an earlier, higher observation."""

from __future__ import annotations

from datetime import date

import pytest

from farmcore.core.agro.phenology import StageEntry, StageWindows, stage_on

WINDOWS = StageWindows(
    ((date(2025, 4, 1), 9), (date(2025, 4, 15), 13), (date(2025, 5, 26), 61)),
    source="assumption: test phase dates",
)


def test_entries_and_windows_give_the_stage_with_where_it_came_from() -> None:
    entries = [StageEntry(date(2025, 4, 20), 15), StageEntry(date(2025, 5, 10), 57)]

    before = stage_on(date(2025, 3, 30), entries, WINDOWS)
    by_window = stage_on(date(2025, 4, 16), entries, WINDOWS)
    by_entry = stage_on(date(2025, 5, 12), entries, WINDOWS)
    later_window = stage_on(date(2025, 6, 1), entries, WINDOWS)

    assert before is None
    assert (by_window.bbch, by_window.origin, by_window.since) == (13, "window", date(2025, 4, 15))
    assert by_window.source == "assumption: test phase dates"
    assert (by_entry.bbch, by_entry.origin, by_entry.since) == (57, "entry", date(2025, 5, 10))
    assert (later_window.bbch, later_window.origin) == (61, "window")
    assert stage_on(date(2025, 6, 1), entries).origin == "entry"  # no windows: last entry holds


def test_a_later_lower_window_does_not_undo_an_observed_stage() -> None:
    early = [StageEntry(date(2025, 4, 10), 53)]

    got = stage_on(date(2025, 4, 20), early, WINDOWS)  # window says 13 from 15 April

    assert (got.bbch, got.origin, got.since) == (53, "entry", date(2025, 4, 10))
    same_day = stage_on(date(2025, 4, 15), [StageEntry(date(2025, 4, 15), 12)], WINDOWS)
    assert (same_day.bbch, same_day.origin) == (12, "entry")  # an observation wins a tie

    # Neither a later, lower entry nor an entry below the window already in force goes back.
    lower_entry = [StageEntry(date(2025, 4, 10), 61), StageEntry(date(2025, 4, 20), 13)]
    under_window = [StageEntry(date(2025, 4, 16), 12)]
    for day, entries, want in [
        (date(2025, 4, 19), lower_entry, (61, "entry", date(2025, 4, 10))),
        (date(2025, 4, 20), lower_entry, (61, "entry", date(2025, 4, 10))),
        (date(2025, 4, 15), under_window, (13, "window", date(2025, 4, 15))),
        (date(2025, 4, 16), under_window, (13, "window", date(2025, 4, 15))),
    ]:
        got = stage_on(day, entries, WINDOWS)
        assert (got.bbch, got.origin, got.since) == want, day


def test_invalid_stages_and_windows_are_refused() -> None:
    with pytest.raises(ValueError, match="0-99"):
        StageEntry(date(2025, 4, 1), 100)
    with pytest.raises(ValueError, match="ascending"):
        StageWindows(((date(2025, 5, 1), 61), (date(2025, 4, 1), 9)), source=WINDOWS.source)
    with pytest.raises(ValueError, match="ascending"):  # stages never go backwards
        StageWindows(((date(2025, 4, 1), 61), (date(2025, 5, 1), 9)), source=WINDOWS.source)
    # A source is a citation (year, DOI or link) or a labelled assumption, never an opaque word.
    sourced = StageWindows(((date(2025, 4, 1), 9),), source="IFV Bordeaux phase calendar, 2024")
    assert stage_on(date(2025, 4, 2), windows=sourced).source == sourced.source
    for opaque in ["", "x", "assumption:", "regional calendar"]:
        with pytest.raises(ValueError, match="source"):
            StageWindows(((date(2025, 4, 1), 9),), source=opaque)
