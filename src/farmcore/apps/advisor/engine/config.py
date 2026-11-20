"""Every agronomic threshold of the Spray Advisor, with its value, source and `assumed` flag.

No rule module holds a threshold literal: rules read `config.<name>.value` and echo it in
`RuleResult.threshold`. "assumed" marks defaults that still await agronomist review. The mildew
models' parameters (3-10, Goidanich, Gubler–Thomas, with their sources) are declared in their
ModelSpec modules (`engine/mildew/`); the defaults here are read from those parameters (`_p`), so
a rule and its spec cannot disagree.

Upgrade path: the Orlandini continuous incubation form would add its parameters next to
`incub_table` (the Goidanich table, data rather than a `Threshold`, in
`mildew/downy_incubation.py`). The incubation
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

from farmcore.apps.advisor.engine.mildew import PENDING as _PENDING
from farmcore.apps.advisor.engine.mildew import downy_3_10, downy_incubation, powdery_gubler_thomas
from farmcore.apps.advisor.engine.mildew.downy_3_10 import GATE_SOURCE as _SRC_GATE
from farmcore.apps.advisor.engine.mildew.downy_3_10 import SOURCE as _SRC_3_10
from farmcore.apps.advisor.engine.mildew.downy_incubation import GOIDANICH_TABLE, GoidanichTable
from farmcore.apps.advisor.engine.mildew.downy_incubation import SOURCE as _SRC_GOIDANICH
from farmcore.apps.advisor.engine.mildew.powdery_gubler_thomas import BLOOM_SOURCE as _SRC_BLOOM
from farmcore.apps.advisor.engine.mildew.powdery_gubler_thomas import END_SOURCE as _SRC_POWDERY_END
from farmcore.apps.advisor.engine.mildew.powdery_gubler_thomas import POWDERY_RESISTANCE_DAYS
from farmcore.apps.advisor.engine.types import ProductRef, RuleResult
from farmcore.core.model_spec import Parameter

DEFAULT_REGION = "FRI12"  # NUTS-3 Gironde (St-Émilion station 33394002)

_SRC_PROTECTION = (
    "République et canton de Genève, annexe 22 (ge.ch/document/2937/annexe/22; that address "
    "now serves another document, a dated copy is still to be pinned): "
    "contact 8–10 d, penetrant 10–12 d, 7–8 d in rapid growth"
)
_SRC_PHASE = "assumption: rapid-growth phase dates (shoot growth to pea size) per year; " + _PENDING
_SRC_ARRETE = (
    "Arrêté du 4 mai 2017 modifié (FR; the 8 mm/h rain rule is from the 27 Dec 2019 amendment), "
    "via INERIS aida.ineris.fr/node/43636"
)
_SRC_RAINFAST = (
    "no rain ≥ 2 h after spraying: Regione Marche agromet bulletin; Nita 2020 (Virginia Tech)"
)


_SRC_BUNCH_CLOSURE = (
    "assumption: bunch closure (BBCH 77) dated per region and year, the calendar programme's "
    "end; " + _PENDING
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


def _p(parameter: Parameter) -> Any:
    """The threshold a mildew ModelSpec parameter sets (`engine/mildew/`)."""
    return _t(
        parameter.value, parameter.source, parameter.unit, assumed=parameter.status == "assumed"
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

    # Downy primary infection, 3-10 rule on rolling hourly windows (`mildew/downy_3_10.py`).
    downy_window_h: Threshold[int] = _p(downy_3_10.P["downy_window_h"])
    downy_rain_min_mm: Threshold[float] = _p(downy_3_10.P["downy_rain_min_mm"])
    downy_temp_min_c: Threshold[float] = _p(downy_3_10.P["downy_temp_min_c"])
    downy_event_max_days: Threshold[float] = _p(downy_3_10.P["downy_event_max_days"])

    # Downy incubation after each 3-10 event: Goidanich table, accumulated hourly
    # (`mildew/downy_incubation.py`).
    incub_table: GoidanichTable = GOIDANICH_TABLE
    incub_tmin: Threshold[float] = _p(downy_incubation.P["incub_tmin"])
    incub_humid_rh_hourly: Threshold[float] = _p(downy_incubation.P["incub_humid_rh_hourly"])
    incub_alert_pct: Threshold[float] = _p(downy_incubation.P["incub_alert_pct"])
    incub_symptoms_pct: Threshold[float] = _p(downy_incubation.P["incub_symptoms_pct"])

    # Powdery: Gubler–Thomas index on local days from bloom to `powdery_end` (bloom + 28 d)
    # (`mildew/powdery_gubler_thomas.py`).
    gt_band_min_c: Threshold[float] = _p(powdery_gubler_thomas.P["gt_band_min_c"])
    gt_band_max_c: Threshold[float] = _p(powdery_gubler_thomas.P["gt_band_max_c"])
    gt_min_hours: Threshold[int] = _p(powdery_gubler_thomas.P["gt_min_hours"])
    gt_hot_c: Threshold[float] = _p(powdery_gubler_thomas.P["gt_hot_c"])
    gt_onset: Threshold[int] = _p(powdery_gubler_thomas.P["gt_onset"])
    gt_step_pre_onset: Threshold[int] = _p(powdery_gubler_thomas.P["gt_step_pre_onset"])
    gt_delta_qualify: Threshold[int] = _p(powdery_gubler_thomas.P["gt_delta_qualify"])
    gt_delta_qualify_hot: Threshold[int] = _p(powdery_gubler_thomas.P["gt_delta_qualify_hot"])
    gt_delta_fail: Threshold[int] = _p(powdery_gubler_thomas.P["gt_delta_fail"])
    gt_delta_fail_hot: Threshold[int] = _p(powdery_gubler_thomas.P["gt_delta_fail_hot"])
    gt_index_min: Threshold[int] = _p(powdery_gubler_thomas.P["gt_index_min"])
    gt_index_max: Threshold[int] = _p(powdery_gubler_thomas.P["gt_index_max"])
    gt_low_max: Threshold[int] = _p(powdery_gubler_thomas.P["gt_low_max"])
    gt_moderate_max: Threshold[int] = _p(powdery_gubler_thomas.P["gt_moderate_max"])
    gt_interval_low_days: Threshold[float] = _p(powdery_gubler_thomas.P["gt_interval_low_days"])
    gt_interval_moderate_days: Threshold[float] = _p(
        powdery_gubler_thomas.P["gt_interval_moderate_days"]
    )
    gt_interval_high_days: Threshold[float] = _p(powdery_gubler_thomas.P["gt_interval_high_days"])

    # Tank-mix: sulphur rides a due downy pass when the powdery cover is about to end.
    tankmix_cover_ending_days: Threshold[float] = _t(2.0, _SRC_TANKMIX, "d", assumed=True)

    # Protection length by product type and phase.
    protection_contact_days: Threshold[float] = _t(8.0, _SRC_PROTECTION, "d")
    protection_penetrant_days: Threshold[float] = _t(10.0, _SRC_PROTECTION, "d")
    protection_rapid_growth_days: Threshold[float] = _t(7.0, _SRC_PROTECTION, "d")

    # Wash-off of contact covers: cumulative rain since the spray (copper = downy contact cover,
    # sulphur = powdery contact cover).
    washoff_copper_mm: Threshold[float] = _t(
        20.0,
        "Pleinchamp / Réussir (IFV series): copper renewed after ~20 mm, "
        "half lost in the first 5 mm",
        "mm",
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
        32.0,
        "AWRI eBulletin Oct 2021: sulphur phytotoxicity risk > 32 °C with RH > 70 % "
        "(applied here whatever the humidity)",
        "°C",
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
