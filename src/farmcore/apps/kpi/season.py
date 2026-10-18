"""The station seasons the KPIs are computed from: `simulate` + `calendar_passes` per year.

`season_results(path, years)` reads the open station file (`stations.connector.read_hours`),
builds `HourlyObs`, and per year runs `simulate(series, cfg, *season_span(cfg))` and
`calendar_passes(cfg, year)` with `cfg = AdvisorConfig.for_region_year(REGION, year)`: the shared
span, so model and calendar compare like for like.

Cached per process on `(path, mtime_ns, year)`: a season simulates once per file version, and
editing an assumption never re-simulates (assumptions only enter `service.kpis`).

Replay-safety exception (labelled on the page, `config.RETROSPECTIVE_LABEL`): the whole station
season is replayed from the file, whatever the replay clock. Only the voice count is clock-bounded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import (
    CalendarResult,
    SeasonResult,
    calendar_passes,
    season_span,
    simulate,
)
from farmcore.apps.advisor.engine.types import HourlyObs
from farmcore.apps.kpi import config
from farmcore.apps.stations.connector import FILE, read_hours

__all__ = ["FILE", "SeasonYear", "season_results", "station_file"]

CALENDAR_INPUTS = (  # the `CalendarConfig` thresholds that shape the calendar's spray dates
    "downy_interval_days",
    "downy_interval_rapid_growth_days",
    "sulphur_interval_days",
    "merge_days",
)


@dataclass(frozen=True)
class SeasonYear:
    year: int
    model: SeasonResult
    calendar: CalendarResult
    copper: str  # the year's cfg.default_downy_product.name (what model and calendar spray)
    sulphur: str  # the year's cfg.default_powdery_product.name
    # The lead figure's conservative calendar: the downy interval all season (`plain_calendar`).
    plain_calendar: CalendarResult
    plain_interval_days: float  # the year's `calendar.downy_interval_days`, for its label
    # Any calendar interval of the year's config is an assumption (`Threshold.assumed`): every
    # figure compared with the calendar is then labelled "assumed" on the page.
    calendar_assumed: bool = True


def plain_calendar(cfg: AdvisorConfig, year: int) -> CalendarResult:
    """`calendar_passes` with the rapid-growth downy interval set to the plain downy interval:
    a fixed-interval copper calendar over the same span (fewer passes)."""
    cal = cfg.calendar
    plain = cal.model_copy(update={"downy_interval_rapid_growth_days": cal.downy_interval_days})
    return calendar_passes(cfg.model_copy(update={"calendar": plain}), year)


def station_file(data_dir: Path) -> Path:
    """The open station file under `data_dir` (`stations.connector.FILE`)."""
    return data_dir / FILE


@lru_cache(maxsize=4)
def _series(path: str, mtime_ns: int) -> tuple[HourlyObs, ...]:
    return tuple(
        HourlyObs(
            ts=h.ts,
            rr1_mm=h.values["rr1_mm"],
            t_c=h.values["t_c"],
            rh_pct=h.values["rh_pct"],
            ff_kmh=h.values["ff_kmh"],
        )
        for h in read_hours(Path(path))
    )


@lru_cache(maxsize=16)
def _season_year(path: str, mtime_ns: int, year: int) -> SeasonYear:
    cfg = AdvisorConfig.for_region_year(config.REGION, year)
    model = simulate(_series(path, mtime_ns), cfg, *season_span(cfg))
    return SeasonYear(
        year=year,
        model=model,
        calendar=calendar_passes(cfg, year),
        copper=cfg.default_downy_product.name,
        sulphur=cfg.default_powdery_product.name,
        plain_calendar=plain_calendar(cfg, year),
        plain_interval_days=cfg.calendar.downy_interval_days.value,
        calendar_assumed=any(getattr(cfg.calendar, n).assumed for n in CALENDAR_INPUTS),
    )


def season_results(path: Path, years: Sequence[int] = config.YEARS) -> dict[int, SeasonYear]:
    """`{year: SeasonYear}` for `years`, from the station file at `path` (must exist)."""
    resolved = path.resolve()
    mtime_ns = resolved.stat().st_mtime_ns
    return {year: _season_year(str(resolved), mtime_ns, year) for year in years}
