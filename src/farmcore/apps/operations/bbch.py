"""Dated grapevine BBCH phases per year, for register rows whose log carries no BBCH.

ASSUMPTION (labelled in every export as `bbch_source = config`): Bordeaux / FRI12 phase dates,
consistent with the advisor's season config (gate ≈ BBCH 13, bloom = BBCH 61, bunch closure =
BBCH 77; 2024 'exceptional earliness', ISVV 2024). Defaults await agronomist review.
A year not listed has no configured phase: the row's BBCH stays blank (`bbch_source = none`).
"""

from __future__ import annotations

from datetime import date

SOURCE = (
    "assumption: grapevine BBCH phase dates per year for Bordeaux (FRI12), aligned with the "
    "advisor season config (gate BBCH 13, bloom BBCH 61, bunch closure BBCH 77); "
    "assumed: awaiting agronomist"
)

# year -> [(first local day of the phase, BBCH code)], ascending.
PHASES: dict[int, tuple[tuple[date, int], ...]] = {
    2024: (
        (date(2024, 3, 25), 9),  # budburst
        (date(2024, 4, 5), 13),  # 3 leaves unfolded (season gate)
        (date(2024, 4, 20), 15),  # 5 leaves unfolded (rapid growth)
        (date(2024, 5, 5), 53),  # inflorescences clearly visible
        (date(2024, 5, 15), 57),  # inflorescences fully developed
        (date(2024, 5, 28), 61),  # beginning of flowering
        (date(2024, 6, 10), 71),  # fruit set
        (date(2024, 6, 20), 75),  # berries pea-sized
        (date(2024, 7, 8), 77),  # bunch closure
        (date(2024, 8, 1), 81),  # veraison
    ),
    2025: (
        (date(2025, 4, 1), 9),
        (date(2025, 4, 15), 13),
        (date(2025, 4, 25), 15),
        (date(2025, 5, 8), 53),
        (date(2025, 5, 16), 57),
        (date(2025, 5, 26), 61),
        (date(2025, 6, 8), 71),
        (date(2025, 6, 18), 75),
        (date(2025, 7, 5), 77),
        (date(2025, 8, 1), 81),
    ),
}


def phase_on(day: date) -> int | None:
    """The configured BBCH on a local date, or None (year not configured, or before budburst)."""
    code = None
    for start, bbch in PHASES.get(day.year, ()):
        if start > day:
            break
        code = bbch
    return code
