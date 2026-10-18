"""Every agronomic threshold of the Spray Advisor, with its value, source and `assumed` flag.

No other engine module holds a threshold literal: rules read `config.<name>.value` and echo it
in `RuleResult.threshold`. "assumed" marks defaults that still await agronomist review.

Upgrade path: the Orlandini continuous incubation form would add its parameters next to
`incub_table` (the Goidanich table, data rather than a `Threshold`). The incubation
(`incub_*`), Gubler–Thomas (`gt_*`, `powdery_start/end` per year) and tank-mix keys are
top-level thresholds.

Two nested blocks are read by `simulate.py` and `daily.py` only: `calendar` (the calendar-pass
estimate and the simulation's daily evaluation hour) and `daily` (the daily engine form on
regional daily data). Rules name their keys dotted (`daily.incub_daily_rh_split`) in
`explain`; `threshold_names()` lists only the top-level (hourly engine) thresholds,
`nested_threshold_names()` the dotted ones.

`washoff_sulphur_mm` is 30 mm; the powdery period ends at bloom + `POWDERY_RESISTANCE_DAYS`
(28 d, Gadoury 2003; derived in `for_region_year` unless `powdery_end` is overridden); bunch
closure is kept per year as `calendar.bunch_closure`, the calendar programme's end; the calendar
downy interval is 7 d in rapid growth, 10 d otherwise. Unsourced values say
"assumed: awaiting agronomist".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from farmcore.apps.advisor.engine.types import ProductRef, RuleResult

DEFAULT_REGION = "FRI12"  # NUTS-3 Gironde (St-Émilion station 33394002)

_PENDING = "assumed: awaiting agronomist"

_SRC_GATE = (
    "assumption: shoots ≥ 10 cm (BBCH 13–15) dated per region and year; Bordeaux usually "
    "mid–late April (BSV Sud Aquitaine); default, " + _PENDING
)
_SRC_3_10 = "3-10 rule (Baldacci 1947) via Gessi et al. 2022, Microorganisms 11:73"
_SRC_PROTECTION = (
    "République et canton de Genève, annexe 22 (ge.ch/document/2937/annexe/22): "
    "contact 8–10 d, penetrant 10–12 d, 7–8 d in rapid growth"
)
_SRC_PHASE = "assumption: rapid-growth phase dates (shoot growth to pea size) per year; " + _PENDING
_SRC_ARRETE = "Arrêté du 4 mai 2017 (FR), via INERIS aida.ineris.fr/node/43636"
_SRC_RAINFAST = (
    "no rain ≥ 2 h after spraying: Regione Marche agromet bulletin; Nita 2020 (Virginia Tech)"
)


_SRC_GOIDANICH = (
    "Goidanich et al. 1957 table via VineSens (Pérez-Expósito et al. 2017, Sensors 17:465, Table 1)"
)
_SRC_GT = (
    "Gubler–Thomas risk index, UC IPM ipm.ucanr.edu/agriculture/grape/powdery-mildew/; "
    "Thomas, Gubler et al. 1994"
)
_SRC_BLOOM = "assumption: bloom (BBCH 61) dated per region and year; " + _PENDING
_SRC_BUNCH_CLOSURE = (
    "assumption: bunch closure (BBCH 77) dated per region and year, the calendar programme's "
    "end; " + _PENDING
)
# Berries resist powdery mildew ~3–4 weeks after bloom, so the Gubler–Thomas period (and the
# sulphur passes it drives) ends at bloom + 28 d, not at bunch closure.
POWDERY_RESISTANCE_DAYS = 28
_SRC_POWDERY_END = (
    f"bloom + {POWDERY_RESISTANCE_DAYS} d (powdery_resistance_days): berries resistant to "
    "powdery mildew 3–4 weeks after bloom (Gadoury 2003); bloom date " + _PENDING
)
_SRC_TANKMIX = (
    "assumption: tank-mix when the powdery cover ends within 2 days of a due downy pass "
    "(~85 % of Gironde treatments are downy+powdery in one period, Agreste NA 2019); " + _PENDING
)


_SRC_CALENDAR = (
    "assumption: calendar programme of fixed protective intervals from shoots ≥ 10 cm to bunch "
    "closure (agronomy.md §6 KPI formulas: 7–10 d downy, 7–14 d sulphur); no published figure, "
    "labelled estimate; " + _PENDING
)
_SRC_CALENDAR_RAPID = (
    "calendar downy interval in rapid growth (shoot growth to pea size), the engine's protection "
    "rule: République et canton de Genève, annexe 22 (7–8 d in rapid growth); calendar reading "
    + _PENDING
)
_SRC_WINDOW_DAY = (
    "assumption: a season-simulation window day (hourly data) needs its open run to start in a "
    "daytime working band, so calm night hours alone do not make every day a window day; a KPI "
    "reading, not a legal limit (night spraying is allowed); " + _PENDING
)
_SRC_DAILY_WINDOW = (
    "derived daily window (agronomy.md §7): a likely workable day, not compliance; daily wind is "
    "a 24 h mean"
)
_SRC_DIURNAL = (
    "derived: synthetic hourly T from daily Tmin/Tmax, two half-cosines (Parton & Logan 1981 "
    "style, agronomy.md §2e daily approximation); hours assumed; " + _PENDING
)


class Threshold[T](BaseModel):
    """One configured value with where it comes from."""

    model_config = ConfigDict(frozen=True)

    value: T
    source: str
    assumed: bool = False
    unit: str = ""


def _t(value: Any, source: str, unit: str = "", *, assumed: bool = False) -> Any:
    return Threshold(value=value, source=source, unit=unit, assumed=assumed)


class GoidanichTable(BaseModel):
    """Daily incubation development (%/day) by temperature: rows of (T °C, dry, humid).

    Dry = RH < 75 % daily mean, humid = RH ≥ 75 % (the table's own split; the hourly engine picks
    the column with `incub_humid_rh_hourly`). Rows are ascending; T above the last row reads the
    last row. Data, not a `Threshold`: rules echo the cells they used in `inputs`.
    """

    model_config = ConfigDict(frozen=True)

    rows: tuple[tuple[float, float, float], ...]
    source: str
    note: str = ""


GOIDANICH_TABLE = GoidanichTable(
    rows=(
        (12.0, 0.0, 5.25),
        (12.5, 4.7, 6.2),
        (13.0, 5.3, 7.1),
        (13.5, 6.0, 8.0),
        (14.0, 6.6, 9.0),
        (14.5, 7.1, 9.7),
        (15.0, 7.6, 10.6),
        (15.5, 8.1, 11.1),
        (16.0, 8.5, 11.7),
        (16.5, 9.3, 12.5),
        (17.0, 10.0, 13.25),
        (17.5, 10.5, 14.3),
        (18.0, 11.1, 15.3),
        (18.5, 11.7, 16.0),
        (19.0, 12.5, 16.6),
        (19.5, 13.4, 18.3),
        (20.0, 14.2, 20.0),
        (20.5, 14.8, 21.0),
        (21.0, 15.3, 22.2),
        (21.5, 16.0, 22.2),
        (22.0, 16.6, 22.2),
        (22.5, 17.3, 23.5),
        (23.0, 18.1, 25.0),
        (23.5, 18.1, 25.0),
        (24.0, 18.1, 25.0),
        (24.5, 17.3, 23.5),
        (25.0, 16.6, 22.2),
    ),
    source=_SRC_GOIDANICH,
    note=(
        "assumed: dry 12.0 °C cell set to 0 (source cell ambiguous); dry 16.5 °C = 9.3 (source "
        "prints 6.3, a typo given its neighbours); 25 °C and above constant. Table form, not the "
        "Orlandini continuous equation (upgrade path: add its parameters here)"
    ),
)


class SeasonDates(BaseModel):
    """Dated phases of one region and year."""

    model_config = ConfigDict(frozen=True)

    season_gate: Threshold[date]
    rapid_growth_start: Threshold[date]
    rapid_growth_end: Threshold[date]
    powdery_start: Threshold[date]  # bloom
    bunch_closure: Threshold[date]  # BBCH 77: lands in `calendar.bunch_closure`


def powdery_end(bloom: Threshold[date]) -> Threshold[date]:
    """End of the powdery period: `bloom` + `POWDERY_RESISTANCE_DAYS` (labelled, assumed)."""
    return _t(
        bloom.value + timedelta(days=POWDERY_RESISTANCE_DAYS),
        _SRC_POWDERY_END,
        "date",
        assumed=True,
    )


def _season(
    gate: date,
    rg_start: date,
    rg_end: date,
    gate_source: str,
    *,
    bloom: date,
    bunch_closure: date,
) -> SeasonDates:
    return SeasonDates(
        season_gate=_t(gate, gate_source, "date", assumed=True),
        rapid_growth_start=_t(rg_start, _SRC_PHASE, "date", assumed=True),
        rapid_growth_end=_t(rg_end, _SRC_PHASE, "date", assumed=True),
        powdery_start=_t(bloom, _SRC_BLOOM, "date", assumed=True),
        bunch_closure=_t(bunch_closure, _SRC_BUNCH_CLOSURE, "date", assumed=True),
    )


# Known (region, year) seasons. Anything else gets `_default_season(year)`.
SEASONS: dict[tuple[str, int], SeasonDates] = {
    ("FRI12", 2024): _season(
        date(2024, 4, 5),
        date(2024, 4, 20),
        date(2024, 6, 30),
        _SRC_GATE + "; 2024 'exceptional earliness' (ISVV 2024)",
        bloom=date(2024, 5, 28),
        bunch_closure=date(2024, 7, 8),
    ),
    ("FRI12", 2025): _season(
        date(2025, 4, 15),
        date(2025, 4, 25),
        date(2025, 6, 30),
        _SRC_GATE,
        bloom=date(2025, 5, 26),
        bunch_closure=date(2025, 7, 5),
    ),
}


def _default_season(year: int) -> SeasonDates:
    return _season(
        date(year, 4, 15),
        date(year, 4, 25),
        date(year, 6, 30),
        _SRC_GATE + "; baseline default 15 Apr",
        bloom=date(year, 6, 1),
        bunch_closure=date(year, 7, 10),
    )


# Per-region defaults: the same engine and thresholds on another wine region's
# regional feed; only the dated season differs. Used when `SEASONS` has no (region, year) entry.
_REGIONS_PENDING = _PENDING


def _region_season(
    region: str,
    gate_note: str,
    *,
    gate: date,
    rg: tuple[date, date],
    bloom: date,
    bunch_closure: date,
) -> SeasonDates:
    """A region's dated season; every source names the region and stays assumed."""
    where = f"{region}: "
    tail = f" ({_REGIONS_PENDING})"
    return SeasonDates(
        season_gate=_t(gate, where + gate_note + tail, "date", assumed=True),
        rapid_growth_start=_t(
            rg[0], where + "rapid-growth start (shoot growth)" + tail, "date", assumed=True
        ),
        rapid_growth_end=_t(
            rg[1], where + "rapid-growth end (pea size)" + tail, "date", assumed=True
        ),
        powdery_start=_t(bloom, where + "bloom (BBCH 61)" + tail, "date", assumed=True),
        bunch_closure=_t(
            bunch_closure,
            where + "bunch closure (BBCH 77), the calendar programme's end" + tail,
            "date",
            assumed=True,
        ),
    )


def _bologna(year: int) -> SeasonDates:
    return _region_season(
        "ITH55 Bologna",
        "shoots ≥ 10 cm by 15 Apr (ARPAE 2024 model bulletins: infections by 23 Apr)",
        gate=date(year, 4, 15),
        rg=(date(year, 4, 20), date(year, 6, 30)),
        bloom=date(year, 5, 25),
        bunch_closure=date(year, 7, 5),
    )


def _valladolid(year: int) -> SeasonDates:
    return _region_season(
        "ES418 Valladolid / Ribera del Duero",
        "shoots ≥ 10 cm by 2 May (D.O. Ribera 2024 report: budbreak 19 Apr, + ~2 weeks)",
        gate=date(year, 5, 2),
        rg=(date(year, 5, 5), date(year, 7, 10)),
        bloom=date(year, 6, 10),
        bunch_closure=date(year, 7, 20),
    )


REGION_DEFAULTS: dict[str, Callable[[int], SeasonDates]] = {
    "ITH55": _bologna,
    "ES418": _valladolid,
}


class CalendarConfig(BaseModel):
    """Calendar-pass estimate and the season simulation's evaluation hour."""

    model_config = ConfigDict(frozen=True)

    bunch_closure: Threshold[date]  # per year, from `SeasonDates`; the calendar's last day
    downy_interval_days: Threshold[float] = _t(
        10.0, _SRC_CALENDAR + ": downy outside rapid growth", "d", assumed=True
    )
    downy_interval_rapid_growth_days: Threshold[float] = _t(
        7.0, _SRC_CALENDAR_RAPID, "d", assumed=True
    )
    sulphur_interval_days: Threshold[float] = _t(14.0, _SRC_CALENDAR, "d", assumed=True)
    merge_days: Threshold[float] = _t(
        5.0,
        "assumption: a sulphur date rides the nearest downy pass within 5 days (tank-mix, "
        "1 pass, 2 treatments); with 7/10 d downy every sulphur date rides; " + _PENDING,
        "d",
        assumed=True,
    )
    eval_hour_local: Threshold[int] = _t(
        6,
        "season simulation: the engine is evaluated once a day at 06:00 local "
        "(the demo replay hour)",
        "h",
    )
    window_day_start_hour_local: Threshold[int] = _t(
        6, _SRC_WINDOW_DAY + ": daytime band starts 06:00 local", "h", assumed=True
    )
    window_day_end_hour_local: Threshold[int] = _t(
        20, _SRC_WINDOW_DAY + ": daytime band ends 20:00 local", "h", assumed=True
    )


class DailyConfig(BaseModel):
    """Daily engine form on regional daily data."""

    model_config = ConfigDict(frozen=True)

    downy_days: Threshold[int] = _t(
        2, _SRC_3_10 + "; daily form: rain and mean T over 2 calendar days (24–48 h)", "d"
    )
    incub_daily_rh_split: Threshold[float] = _t(
        75.0, _SRC_GOIDANICH + "; humid column at daily mean RH ≥ 75 % (the table's own split)", "%"
    )
    window_wind_max_ms: Threshold[float] = _t(
        5.3, _SRC_ARRETE + ": Beaufort 3 ≈ 19 km/h ≈ 5.3 m/s; " + _SRC_DAILY_WINDOW, "m/s"
    )
    window_rain_max_mm: Threshold[float] = _t(
        2.0, _SRC_DAILY_WINDOW + ": rain on the day < 2 mm", "mm", assumed=True
    )
    window_rain_next_max_mm: Threshold[float] = _t(
        5.0, _SRC_DAILY_WINDOW + ": rain on the next day < 5 mm", "mm", assumed=True
    )
    t_min_hour_local: Threshold[int] = _t(
        6, _SRC_DIURNAL + ": Tmin at 06:00 local", "h", assumed=True
    )
    t_max_hour_local: Threshold[int] = _t(
        15, _SRC_DIURNAL + ": Tmax at 15:00 local", "h", assumed=True
    )


class AdvisorConfig(BaseModel):
    """All Spray Advisor thresholds for one region and year. Build with `for_region_year`."""

    model_config = ConfigDict(frozen=True)

    region: str = DEFAULT_REGION
    year: int
    local_tz: str = "Europe/Paris"  # gate and phase dates are local calendar dates

    # Season gate and phases (local dates).
    season_gate: Threshold[date]
    rapid_growth_start: Threshold[date]
    rapid_growth_end: Threshold[date]
    powdery_start: Threshold[date]  # bloom (BBCH 61)
    powdery_end: Threshold[date]  # bloom + POWDERY_RESISTANCE_DAYS (berry resistance)

    # Downy primary infection, 3-10 rule on rolling hourly windows.
    downy_window_h: Threshold[int] = _t(48, _SRC_3_10 + " (≥ 10 mm in 24–48 h)", "h")
    downy_rain_min_mm: Threshold[float] = _t(10.0, _SRC_3_10, "mm")
    downy_temp_min_c: Threshold[float] = _t(10.0, _SRC_3_10 + " (mean T ≥ 10 °C)", "°C")
    downy_event_max_days: Threshold[float] = _t(
        3.0,
        "assumption: an uncovered event stays active risk 3 days; " + _PENDING,
        "d",
        assumed=True,
    )

    # Downy incubation after each 3-10 event: Goidanich table, accumulated hourly.
    incub_table: GoidanichTable = GOIDANICH_TABLE
    incub_tmin: Threshold[float] = _t(
        11.0,
        "assumption: no incubation progress at or below 11 °C, linear ramp to the 12.0 °C row "
        "(Charentes table starts at 12 °C; nothing below ~10–11 °C); " + _PENDING,
        "°C",
        assumed=True,
    )
    incub_humid_rh_hourly: Threshold[float] = _t(
        90.0,
        "assumption: hourly RH ≥ 90 % as a leaf-wetness proxy selects the humid column; "
        + _PENDING,
        "%",
        assumed=True,
    )
    incub_alert_pct: Threshold[float] = _t(
        50.0, "Montenegro DSS alert at GI ≥ 50 % (Sensors 26:2843, PMC13165835)", "%"
    )
    incub_symptoms_pct: Threshold[float] = _t(
        100.0, _SRC_GOIDANICH + "; GI ≥ 100 % = incubation complete, symptoms expected", "%"
    )

    # Powdery: Gubler–Thomas index on local days from bloom to `powdery_end` (bloom + 28 d).
    gt_band_min_c: Threshold[float] = _t(21.0, _SRC_GT + " (70 °F)", "°C")
    gt_band_max_c: Threshold[float] = _t(29.5, _SRC_GT + " (85 °F)", "°C")
    gt_min_hours: Threshold[int] = _t(6, _SRC_GT + "; ≥ 6 continuous hours in the band", "h")
    gt_hot_c: Threshold[float] = _t(
        35.0,
        _SRC_GT + "; ≥ 35 °C (95 °F) for > 15 min; hourly proxy: any hour ≥ 35 °C",
        "°C",
    )
    gt_onset: Threshold[int] = _t(60, _SRC_GT + "; epidemic onset at 60; before it, resets")
    gt_step_pre_onset: Threshold[int] = _t(20, _SRC_GT + "; +20 per qualifying day")
    gt_delta_qualify: Threshold[int] = _t(20, _SRC_GT + "; after onset, ≥ 6 h in band → +20")
    gt_delta_qualify_hot: Threshold[int] = _t(
        10, _SRC_GT + "; after onset, both conditions the same day → +10"
    )
    gt_delta_fail: Threshold[int] = _t(-10, _SRC_GT + "; after onset, < 6 h in band → −10")
    gt_delta_fail_hot: Threshold[int] = _t(
        -20,
        "assumption: after onset, < 6 h in band and a ≥ 35 °C hour → −20 (UC IPM reading, "
        "both penalties); " + _PENDING,
        assumed=True,
    )
    gt_index_min: Threshold[int] = _t(0, _SRC_GT + "; index bounded 0–100")
    gt_index_max: Threshold[int] = _t(100, _SRC_GT + "; index bounded 0–100")
    gt_low_max: Threshold[int] = _t(
        30, _SRC_GT + "; 0–30 low; values above (e.g. 35) map up to moderate"
    )
    gt_moderate_max: Threshold[int] = _t(
        50, _SRC_GT + "; 40–50 moderate; values above (e.g. 55) map up to high"
    )
    gt_interval_low_days: Threshold[float] = _t(
        18.0,
        "assumption: midpoint of UC IPM sulphur interval 14–21 d at low risk; " + _PENDING,
        "d",
        assumed=True,
    )
    gt_interval_moderate_days: Threshold[float] = _t(
        14.0,
        "assumption: within UC IPM sulphur interval 10–17 d at moderate risk ; " + _PENDING,
        "d",
        assumed=True,
    )
    gt_interval_high_days: Threshold[float] = _t(
        7.0, _SRC_GT + "; sulphur interval 7 d at high risk (≥ 60)", "d"
    )

    # Tank-mix: sulphur rides a due downy pass when the powdery cover is about to end.
    tankmix_cover_ending_days: Threshold[float] = _t(2.0, _SRC_TANKMIX, "d", assumed=True)

    # Protection length by product type and phase.
    protection_contact_days: Threshold[float] = _t(8.0, _SRC_PROTECTION, "d")
    protection_penetrant_days: Threshold[float] = _t(10.0, _SRC_PROTECTION, "d")
    protection_rapid_growth_days: Threshold[float] = _t(7.0, _SRC_PROTECTION, "d")

    # Wash-off of contact covers: cumulative rain since the spray (copper = downy contact cover,
    # sulphur = powdery contact cover).
    washoff_copper_mm: Threshold[float] = _t(
        20.0, "Pleinchamp / IFV: copper renewed after ~20 mm, half lost in the first 5 mm", "mm"
    )
    washoff_sulphur_mm: Threshold[float] = _t(
        30.0,
        "sulphur renewed after ~30 mm rain: Ticino agronomy 30–60 mm, Valle d'Aosta 30–40 mm; "
        "low end taken. French sources say less: "
        'BIVB Mémo BIO 2025 ("lessivage dès 20-25 mm"), Chambre d\'agriculture AURA (Oct 2024, '
        "20 mm); 30 mm kept; " + _PENDING,
        "mm",
        assumed=True,
    )

    # Spray window (hourly).
    window_wind_max_kmh: Threshold[float] = _t(
        19.0, _SRC_ARRETE + ": wind ≤ Beaufort 3 (FF 10-min mean, not gusts)", "km/h"
    )
    window_rr1_max_mm_h: Threshold[float] = _t(
        8.0, _SRC_ARRETE + ": no application when rain > 8 mm/h", "mm/h"
    )
    window_rain_lookahead_h: Threshold[int] = _t(2, _SRC_RAINFAST, "h")
    window_rain_next_max_mm: Threshold[float] = _t(
        0.5,
        "assumption: 'no rain' in the next hours = sum < 0.5 mm (0.2 mm gauge tip passes); "
        + _PENDING,
        "mm",
        assumed=True,
    )
    window_sulphur_tmax_c: Threshold[float] = _t(
        32.0, "AWRI eBulletin Oct 2021: sulphur phytotoxicity risk > 32 °C", "°C"
    )
    window_min_open_hours: Threshold[int] = _t(2, _SRC_RAINFAST + " (≥ 2 open hours)", "h")
    window_horizon_h: Threshold[int] = _t(
        72, "assumption: look for the next window over 3 days", "h", assumed=True
    )

    # Nested blocks: calendar estimate + simulation hour, daily engine form.
    calendar: CalendarConfig  # built per year by `for_region_year` (carries bunch closure)
    daily: DailyConfig = DailyConfig()

    default_downy_product: ProductRef = ProductRef(
        name="copper", product_type="contact", targets=("downy",)
    )
    default_powdery_product: ProductRef = ProductRef(
        name="sulphur", product_type="contact", targets=("powdery",)
    )

    @classmethod
    def for_region_year(cls, region: str, year: int, **overrides: Any) -> AdvisorConfig:
        key = region.upper()
        season = SEASONS.get((key, year)) or REGION_DEFAULTS.get(key, _default_season)(year)
        values: dict[str, Any] = dict(season)
        bunch_closure = values.pop("bunch_closure")
        values["calendar"] = CalendarConfig(bunch_closure=bunch_closure)
        values.update(overrides)
        values.setdefault("powdery_end", powdery_end(values["powdery_start"]))
        return cls(region=region.upper(), year=year, **values)

    def threshold(self, name: str) -> Threshold:
        """The named threshold; a dotted name reads a nested block (`daily.downy_days`)."""
        obj: Any = self
        for part in name.split("."):
            obj = getattr(obj, part)
        return obj

    def thresholds(self, *names: str) -> dict[str, Any]:
        """`{name: value}` for the named thresholds, as echoed in `RuleResult.threshold`."""
        return {name: self.threshold(name).value for name in names}

    def sources(self, *names: str) -> str:
        """Distinct sources of the named thresholds, in order."""
        seen: list[str] = []
        for name in names:
            src = self.threshold(name).source
            if src not in seen:
                seen.append(src)
        return "; ".join(seen)

    def assumed(self, *names: str) -> list[str]:
        return [name for name in names if self.threshold(name).assumed]

    def explain(
        self,
        rule: str,
        names: tuple[str, ...],
        inputs: dict[str, Any],
        verdict: str,
        note: str = "",
    ) -> RuleResult:
        """A `RuleResult` echoing the named thresholds and sources; assumed ones in the note."""
        assumed = self.assumed(*names)
        if assumed:
            note = f"{note} (assumed: {', '.join(assumed)})".strip()
        return RuleResult(
            rule=rule,
            inputs=inputs,
            threshold=self.thresholds(*names),
            verdict=verdict,
            source=self.sources(*names),
            note=note,
        )

    @classmethod
    def threshold_names(cls) -> list[str]:
        """Every top-level field that is a `Threshold` (the hourly engine's)."""
        return _threshold_fields(cls)

    @classmethod
    def nested_threshold_names(cls) -> list[str]:
        """Every threshold of the nested blocks, dotted (`calendar.merge_days`, `daily.*`)."""
        return [
            f"{block}.{name}"
            for block, model in (("calendar", CalendarConfig), ("daily", DailyConfig))
            for name in _threshold_fields(model)
        ]


def _threshold_fields(model: type[BaseModel]) -> list[str]:
    return [
        name
        for name, info in model.model_fields.items()
        if isinstance(info.annotation, type) and issubclass(info.annotation, Threshold)
    ]
