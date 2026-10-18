"""Backtest over Météo-France 33394002, 2020–2023 (France only): the pure engine, no database.

Per season: the advisor's simulated passes against the existing calendar programme (same span,
`season.py`'s method), and the model's first downy alert against the BSV Nord Aquitaine
first-symptom window. A backtest, not validation: BSV dates are first visible symptoms across a
three-department network, not infection dates at the station, so the comparison is "consistent /
not consistent with regional first symptoms", never an accuracy figure.

The comparison uses the engine's own Goidanich incubation crossing for the first alert's event
(the predicted first-symptom date) and one assumed tolerance, `TOLERANCE_DAYS`. The 2020–2023
season dates are the config's default season (gate 15 Apr, assumed): both flagged on screen.
Missing source hours are counted, never filled. Spain is not backtested (InfoRiego unverified).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.gate import local_midnight_utc
from farmcore.apps.advisor.engine.simulate import (
    calendar_passes,
    first_downy_alert,
    season_span,
    simulate,
)
from farmcore.apps.advisor.engine.types import HourlyObs
from farmcore.apps.kpi import config
from farmcore.apps.stations.connector import read_hours

SEASON_FILE = Path(__file__).parent / "seasons" / "meteofrance_33394002_2020_2023_marsep.csv"
YEARS: tuple[int, ...] = (2020, 2021, 2022, 2023)

LABEL = "backtest, not validation"
CONSISTENCY_LABEL = (
    "consistent / not consistent with regional first symptoms "
    "(symptoms, not infection dates; network-wide)"
)
TOLERANCE_DAYS = 7
ASSUMPTION_NOTE = (
    f"Consistent means the model's predicted first downy symptom date (its first alert event "
    f"plus the Goidanich incubation) falls within {TOLERANCE_DAYS} days of the BSV first-symptom "
    f"window. The {TOLERANCE_DAYS}-day tolerance is an assumption, agronomist to confirm. "
    f"2020–2023 use the default season dates (gate 15 Apr), also an assumption, agronomist to "
    f"confirm."
)
DISCLAIMER = (
    "BSV dates are first visible symptoms across the Nord Aquitaine network (Gironde, Dordogne, "
    "Lot-et-Garonne), not infection dates at station 33394002. This is not a score of the model."
)
BSV_PROVENANCE = (
    "BSV dates from public bulletins via secondary research; exact days not all verified"
)
DATA_GAP_SUFFIX = " (data gap: earlier alert not ruled out)"
CONSISTENT = "consistent"
NOT_CONSISTENT = "not consistent"
NO_WINDOW = "no comparable window"

SOURCE_LINE = (
    "Météo-France, Données climatologiques de base - horaires, station 33394002 (ST EMILION), "
    "Licence Ouverte / Etalab 2.0"
)


@dataclass(frozen=True)
class BsvRow:
    year: int
    quote: str  # the first downy mildew cell of the published bulletin, verbatim
    source: str
    url: str
    window: tuple[date, date] | None  # the dates the quote states; None: period only
    precision: str = ""  # how exact the bulletin date is, when it is not a stated day

    @property
    def comparable(self) -> bool:
        return self.window is not None


_FREDON = "https://www.fredon.fr/nouvelle-aquitaine/ressources/bsv-vigne-edition-nord-aquitaine"
BSV: dict[int, BsvRow] = {
    2020: BsvRow(
        2020,
        "**15 April 2020** (single isolated plot, Centre-Médoc) and **23–27 April 2020** (first "
        "localized leaf spots in Centre-Médoc, Libournais, Entre-deux-Mers; **28 April 2020** on "
        "Libournais TNT; **3–4 May 2020** generalized leaf emergence; **12 May 2020** for 50% TNT "
        "incidence)",
        "BSV Vigne Nord Aquitaine N°22 Bilan du 22/12/2020",
        _FREDON,
        (date(2020, 4, 15), date(2020, 4, 27)),
    ),
    2021: BsvRow(
        2021,
        "**Mid-May 2021 (week of 17–20 May 2021)** following post-frost May rains (none yet in "
        "*BSV N°5 du 04/05/2021*; regional synthesis around **1 June 2021**). Exact day and "
        "late-May bulletin PDF URL **not verified**.",
        "BSV Vigne Nord Aquitaine N°5 du 04/05/2021 (no symptoms yet) and 2021 synthesis",
        "https://fredon.fr/nouvelle-aquitaine/sites/default/files/BSV/2021/"
        "BSV_Vigne_Nord_Aquitaine_2021_N05.pdf",
        (date(2021, 5, 17), date(2021, 5, 20)),
        "week-level (exact day not verified)",
    ),
    2022: BsvRow(
        2022,
        "**Late April to early May 2022** (initial isolated spots; broader TNT appearance "
        "delayed to **late May / early June 2022** by spring drought) — period only, exact day "
        "and individual bulletin number **not verified**",
        "BSV Vigne Nord Aquitaine 2022 (DRAAF Nouvelle-Aquitaine; Vinopôle): period only",
        "https://draaf.nouvelle-aquitaine.agriculture.gouv.fr/vigne-nord-aquitaine-a2097.html",
        None,
        "period only (exact day not verified)",
    ),
    2023: BsvRow(
        2023,
        "**20 April 2023** (first sentinel TNT spots) and **late April / 28 April – 3 May "
        "2023** (reference leaves, with epidemic acceleration reported in *BSV N°8 du "
        "23/05/2023*)",
        "BSV Vigne Nord Aquitaine N°20 Bilan du 20/12/2023",
        _FREDON,
        (date(2023, 4, 20), date(2023, 5, 3)),
    ),
    2024: BsvRow(
        2024,
        "**16 April 2024** (outside BSV network), **23 April 2024** (official BSV network "
        "leaves), and **30 April 2024** (first inflorescence/leaf attacks)",
        "Vinopôle, Retour sur la campagne 2024",
        "https://www.vinopole.com/ressources/retour-sur-la-campagne-2024/",
        (date(2024, 4, 16), date(2024, 4, 23)),
    ),
    2025: BsvRow(
        2025,
        "**Late April to early May 2025** (oospore germination 31 March at Villenave-d'Ornon; "
        "first leaf spots on early plots in Saint-Émilion plain/Libournais and Graves in late "
        "April / early May; period only, exact day **not verified**)",
        "Vinopôle, Bilan de campagne Girondin 2025",
        "https://www.vinopole.com/ressources/bilan-de-campagne-girondin-2025-comprendre-pour-"
        "mieux-anticiper/",
        None,
        "period only (exact day not verified)",
    ),
}


def verdict(predicted_symptoms: date | None, row: BsvRow) -> str:
    """Consistent when the predicted first-symptom date is within the assumed tolerance of the
    BSV window; a period-only year has no window to compare."""
    if row.window is None:
        return NO_WINDOW
    if predicted_symptoms is None:
        return NOT_CONSISTENT
    first, last = row.window
    slack = timedelta(days=TOLERANCE_DAYS)
    return CONSISTENT if first - slack <= predicted_symptoms <= last + slack else NOT_CONSISTENT


@dataclass(frozen=True)
class BacktestYear:
    year: int
    start: date  # season gate
    end: date  # bunch closure
    model_passes: int
    model_treatments: int
    calendar_passes: int
    calendar_treatments: int
    first_alert: date | None
    event: datetime | None
    predicted_symptoms: date | None
    bsv: BsvRow
    verdict: str
    rain_gap_hours: int  # hours in the span with no rain value at the source (never filled)
    rain_gap_before_alert: int  # of those, the hours before the first alert's evaluation
    rh_gap_hours: int  # hours in the span with no humidity value (incubation skips them)
    rh_gap_extract_hours: int  # the same over the whole Mar-Sep extract (incl. before the gate)
    gap_notes: tuple[str, ...]
    season_assumed: bool


@lru_cache(maxsize=2)
def _series(path: str) -> tuple[HourlyObs, ...]:
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


def _missing_hours(series, cfg, start: date, end: date, field: str) -> list[datetime]:
    """UTC hours in [start, end] (local days) with no value for `field`; never filled."""
    first = local_midnight_utc(start, cfg.local_tz)
    last = local_midnight_utc(end + timedelta(days=1), cfg.local_tz)
    have = {o.ts for o in series if first <= o.ts < last and getattr(o, field) is not None}
    return [
        first + timedelta(hours=i)
        for i in range(int((last - first) / timedelta(hours=1)))
        if first + timedelta(hours=i) not in have
    ]


def _runs(hours: list[datetime]) -> list[tuple[datetime, datetime]]:
    runs: list[tuple[datetime, datetime]] = []
    for h in hours:
        if runs and h - runs[-1][1] == timedelta(hours=1):
            runs[-1] = (runs[-1][0], h)
        else:
            runs.append((h, h))
    return runs


def hours(n: int) -> str:
    return f"{n} hour" if n == 1 else f"{n} hours"


def _span_text(run: tuple[datetime, datetime]) -> str:
    first, last = run[0].astimezone(UTC), run[1].astimezone(UTC)
    if first == last:
        return f"{first:%d %b %H:%M} UTC"
    return f"{first:%d %b %H:%M} – {last:%d %b %H:%M} UTC"


def run_season(year: int, path: Path = SEASON_FILE) -> BacktestYear:
    if year not in BSV:
        raise ValueError(f"no backtest data for {year}; seasons: {', '.join(map(str, BSV))}")
    series = _series(str(path))
    cfg = AdvisorConfig.for_region_year(config.REGION, year)
    start, end = season_span(cfg)
    model = simulate(series, cfg, start, end)
    cal = calendar_passes(cfg, year)
    alert = first_downy_alert(series, cfg, start, end)
    zone = ZoneInfo(cfg.local_tz)
    predicted = alert.symptoms_at.astimezone(zone).date() if alert and alert.symptoms_at else None
    row = BSV[year]
    rain = _missing_hours(series, cfg, start, end, "rr1_mm")
    humidity = _missing_hours(series, cfg, start, end, "rh_pct")
    extract = (datetime(year, 3, 1, tzinfo=UTC), datetime(year, 10, 1, tzinfo=UTC))
    humidity_all = [
        h
        for h in _missing_hours(series, cfg, date(year, 3, 1), date(year, 9, 30), "rh_pct")
        if extract[0] <= h < extract[1]
    ]
    alert_at = (
        datetime.combine(alert.day, time(cfg.calendar.eval_hour_local.value), tzinfo=zone)
        if alert
        else None
    )
    before = [h for h in rain if alert_at is None or h < alert_at]
    notes: list[str] = []
    if rain:
        where = "; ".join(_span_text(r) for r in _runs(rain)[:3])
        notes.append(
            f"No rain value for {where} ({hours(len(rain))}, not filled)"
            + ("; an earlier 3-10 trigger cannot be ruled out." if before else ".")
        )
    if humidity_all:
        first, last = humidity_all[0], humidity_all[-1]
        a, z = first.astimezone(UTC), last.astimezone(UTC)
        where = f"{a:%d %b} UTC" if a.date() == z.date() else f"{a:%d %b} – {z:%d %b} UTC"
        inside = (
            f"{len(humidity)} of them inside the evaluated season span; the incubation step "
            "skips those hours"
            if humidity
            else f"none inside the evaluated season span (it starts at the {start:%d %b} season "
            "gate), so incubation is not affected here"
        )
        notes.append(
            f"{hours(len(humidity_all))} without a humidity value in the Mar–Sep extract "
            f"({where}, not filled); {inside}."
        )
    base = verdict(predicted, row)
    return BacktestYear(
        year=year,
        start=start,
        end=end,
        model_passes=model.passes,
        model_treatments=model.treatments,
        calendar_passes=cal.passes,
        calendar_treatments=cal.treatments,
        first_alert=alert.day if alert else None,
        event=alert.event if alert else None,
        predicted_symptoms=predicted,
        bsv=row,
        verdict=base + DATA_GAP_SUFFIX if before and base != NO_WINDOW else base,
        rain_gap_hours=len(rain),
        rain_gap_before_alert=len(before),
        rh_gap_hours=len(humidity),
        rh_gap_extract_hours=len(humidity_all),
        gap_notes=tuple(notes),
        season_assumed=bool(cfg.season_gate.assumed),
    )


def run_all(path: Path = SEASON_FILE) -> list[BacktestYear]:
    return [run_season(year, path) for year in YEARS]


def display(quote: str) -> str:
    """The verbatim quote without its markdown emphasis marks."""
    return quote.replace("**", "").replace("*", "")


# --- "Tested on N seasons" (Season & impact, Simple view): one compact row per season ---------

TESTED_TITLE = "seasons"
CHECK_CONSISTENT = "consistent"
CHECK_NOT_CONSISTENT = "not consistent"
CHECK_NOT_COMPARABLE = "not comparable"
CHECK_DATA_GAP = "data gap"
TESTED_FOOTNOTES = (
    "Bulletin check: the model's predicted first downy symptom date against the first symptoms "
    "in the regional bulletin (BSV Nord Aquitaine). BSV dates are first visible symptoms, not "
    "infection dates, and are network-wide (Gironde, Dordogne, Lot-et-Garonne), not at the "
    "station.",
    f"Consistent means within {TOLERANCE_DAYS} days of the bulletin window. The "
    f"{TOLERANCE_DAYS}-day tolerance and the 15 Apr season gate are assumptions, agronomist to "
    "confirm.",
    "Not comparable: the bulletin gives a period only, no day window.",
)


@dataclass(frozen=True)
class TestedSeason:
    __test__ = False  # not a pytest class

    year: int
    advisor_passes: int
    calendar_passes: int
    check: str
    note: str  # the season's own caveat (rain gap, period-only bulletin), or ""
    dates: str = ""  # the factual dates line of a comparable season, or ""

    @property
    def saved(self) -> int:
        return self.calendar_passes - self.advisor_passes


def check_label(r: BacktestYear) -> str:
    if r.verdict.endswith(DATA_GAP_SUFFIX):
        return CHECK_DATA_GAP
    return {
        CONSISTENT: CHECK_CONSISTENT,
        NOT_CONSISTENT: CHECK_NOT_CONSISTENT,
        NO_WINDOW: CHECK_NOT_COMPARABLE,
    }[r.verdict]


def _season_note(r: BacktestYear) -> str:
    if r.verdict.endswith(DATA_GAP_SUFFIX):
        return (
            f"{r.year}: {hours(r.rain_gap_hours)} without a rain value at the source (not "
            "filled); an earlier alert cannot be ruled out."
        )
    if r.bsv.window is None:
        return f"{r.year}: the bulletin gives a period only, exact day not verified."
    return ""


def _day(d: date) -> str:
    return f"{d.day} {d:%b}"


def dates_line(r: BacktestYear) -> str:
    """Dates only, no verdict: the first alert, the bulletin's first symptoms (the start of its
    window) and the model's estimated symptom date, with where that falls against the window.
    Empty for a period-only year (no window to place the dates against)."""
    if r.bsv.window is None:
        return ""
    first, last = r.bsv.window
    alert = _day(r.first_alert) if r.first_alert else "none"
    if r.predicted_symptoms is None:
        estimate, where = "none", ""
    else:
        p = r.predicted_symptoms
        estimate = _day(p)
        if p > last:
            n = (p - last).days
            where = f" ({n} day{'s' if n != 1 else ''} after the bulletin window)"
        elif p < first:
            n = (first - p).days
            where = f" ({n} day{'s' if n != 1 else ''} before the bulletin window)"
        else:
            where = " (inside the bulletin window)"
    return (
        f"First alert {alert} · bulletin first symptoms {_day(first)} · "
        f"model estimate {estimate}{where}"
    )


def tested_seasons(station_file: Path | None) -> list[TestedSeason]:
    """2020-2023 from the bundled backtest extract; 2024-2025 from the open station file the KPI
    season runs use (skipped when it is absent). Same engine, same calendar, same BSV rows."""
    runs = [run_season(year) for year in YEARS]
    if station_file is not None and station_file.is_file():
        runs += [run_season(year, station_file) for year in (2024, 2025)]
    return [
        TestedSeason(
            r.year,
            r.model_passes,
            r.calendar_passes,
            check_label(r),
            _season_note(r),
            dates_line(r),
        )
        for r in runs
    ]
