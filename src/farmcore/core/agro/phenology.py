"""BBCH growth stage on a day, from the grower's dated entries or a dated window of phases.

BBCH codes are 0-99 (Meier, U. (ed.) 2018, Growth stages of mono- and dicotyledonous plants,
BBCH Monograph, Julius Kühn-Institut, doi:10.5073/20180906-074619). An entry is an observed
stage on a date. Windows are phases with a first day each, each running until the next starts;
their dates are an assumption or a regional calendar, so they carry a `source`.

Each day an entry or phase starts, the latest entry by then holds, unless a window phase started
after that entry and is a later stage (an observation wins a tie on the same day). The stage on a
day is the highest of those up to it: stages only move forward, so neither a calendar nor a
later, lower entry undoes a stage already reached. The result says where it came from.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from farmcore.core.agro._provenance import ASSUMPTION, provenance_problem


def _check(bbch: int) -> None:
    if isinstance(bbch, bool) or not isinstance(bbch, int) or not 0 <= bbch <= 99:
        raise ValueError(f"BBCH must be an integer 0-99, got {bbch!r}")


@dataclass(frozen=True, slots=True)
class StageEntry:
    on: date
    bbch: int

    def __post_init__(self) -> None:
        _check(self.bbch)


@dataclass(frozen=True, slots=True)
class StageWindows:
    phases: tuple[tuple[date, int], ...]  # (first day, BBCH), ascending in both
    source: str  # a regional calendar's citation, or an 'assumption: ...' label

    def __post_init__(self) -> None:
        status = "assumed" if self.source.strip().lower().startswith(ASSUMPTION) else "sourced"
        if problem := provenance_problem(self.source, status):
            raise ValueError(f"stage windows source {self.source!r}: {problem}")
        for _, bbch in self.phases:
            _check(bbch)
        for (d0, b0), (d1, b1) in zip(self.phases, self.phases[1:], strict=False):
            if not (d0 < d1 and b0 <= b1):
                raise ValueError("stage windows must be ascending by date and by stage")


@dataclass(frozen=True, slots=True)
class Stage:
    bbch: int
    origin: str  # entry | window
    since: date  # the entry's date or the window's first day
    source: str  # the windows' source; "" for an entry


def stage_on(
    day: date, entries: Iterable[StageEntry] = (), windows: StageWindows | None = None
) -> Stage | None:
    """The BBCH stage on `day`, or None when nothing is known by then."""
    known = [e for e in entries if e.on <= day]
    phases = [p for p in windows.phases if p[0] <= day] if windows else []
    best = None
    for d in sorted({e.on for e in known} | {start for start, _ in phases}):
        held = _held(d, known, phases, windows)
        if best is None or held.bbch > best.bbch:  # the first day a stage is reached keeps it
            best = held
    return best


def _held(
    day: date, entries: list[StageEntry], phases: list[tuple[date, int]], windows: StageWindows
) -> Stage:
    """The stage a day's own evidence gives, before the never-backwards rule."""
    known = [e for e in entries if e.on <= day]
    entry = max(known, key=lambda e: (e.on, e.bbch)) if known else None
    phase = max((p for p in phases if p[0] <= day), default=None)
    if phase and (entry is None or (phase[0] > entry.on and phase[1] > entry.bbch)):
        return Stage(phase[1], "window", phase[0], windows.source)
    return Stage(entry.bbch, "entry", entry.on, "")
