"""KPIs from one replayed season (pure, no DB, clock or I/O).

`kpis(model, calendar, voice_ops, area_ha, a, copper=…, sulphur=…)` (the product names the
season's config sprays, `season.SeasonYear.copper` / `.sulphur`):

- treatments avoided = Agreste 20.1 − model treatments (a tank-mixed pass counts 2);
- passes avoided = calendar passes − model passes (the calendar is an estimate);
- €/ha = passes avoided × `pass_cost_eur_ha` + treatments avoided × `product_cost_eur_ha`;
- diesel L/ha = passes avoided × `diesel_l_ha_pass`; CO₂e kg/ha = diesel × `co2_kg_per_l`;
- Cu / S kg/ha = (calendar − model copper / sulphur treatments) × the dose per treatment
  (vs the rapid-growth calendar: Agreste has no copper/sulphur split);
- minutes saved = voice operations × (`form_minutes` − `voice_minutes`);
- farm totals (€, diesel, CO₂e) = the per-ha value × `area_ha`.

Negative values (a model worse than the baseline) are returned as they are, never clamped.
Every `Kpi` carries its formula in words (assumption labels, never config keys) with the numbers
filled in and rounded to the precision the page shows (`Kpi.digits`), and whether it rests on an
assumed value (`Kpi.assumed`: an assumed input, or the calendar programme, itself assumed).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from farmcore.apps.advisor.engine.simulate import CalendarResult, SeasonResult
from farmcore.apps.kpi import config
from farmcore.apps.kpi.config import Assumption, Assumptions

_DIGITS = 6  # round away float noise (20.1 − 12 = 8.100000000000001)
PER_HA_DIGITS = 2  # what the page shows of a per-hectare or per-treatment figure
FARM_DIGITS = 0  # farm totals (€, L, kg CO₂e) in whole units


@dataclass(frozen=True)
class Kpi:
    """One figure. `formula` names its inputs in words with the numbers filled in, rounded to
    `digits` decimals (what the page shows); `assumed` when an input is an assumed value."""

    key: str
    label: str
    value: float
    unit: str
    formula: str
    digits: int = PER_HA_DIGITS
    assumed: bool = False


@dataclass(frozen=True)
class KpiSet:
    model_passes: int
    model_treatments: int
    model_copper: int
    model_sulphur: int
    calendar_passes: int
    calendar_treatments: int
    calendar_copper: int
    calendar_sulphur: int
    agreste_treatments: float
    voice_ops: int
    area_ha: float
    kpis: tuple[Kpi, ...]

    def by_key(self) -> dict[str, Kpi]:
        return {k.key: k for k in self.kpis}


def _count(passes: Iterable[tuple[str, ...]], product: str) -> int:
    return sum(product in products for products in passes)


def _r(x: float) -> float:
    return round(x, _DIGITS)


def fmt(x: float, digits: int = PER_HA_DIGITS) -> str:
    """Fixed notation (never `1e+06`), thousands separated, `digits` decimals at most, trailing
    zeros trimmed; the page's `num` filter prints the same."""
    out = f"{x:,.{digits}f}"
    return out.rstrip("0").rstrip(".") if digits else out


def _input(a: Assumption) -> str:
    """An assumption as the formula prints it: "30 €/ha/pass (machine + labour per pass)"."""
    return f"{fmt(a.value)} {a.unit} ({a.label})"


@dataclass(frozen=True)
class CopperLead:
    """The page's lead figure against one calendar."""

    key: str
    name: str  # the calendar's short name, printed next to every figure ("plain 10-day calendar")
    label: str
    at_least: bool  # the conservative calendar: the figure is a floor
    model_copper: int
    calendar_copper: int
    avoided: int
    pct: float | None  # change vs the calendar, % (negative = fewer); None: no calendar copper
    kg_cu_ha: float
    formula: str
    assumed: bool


def copper_lead(
    model: SeasonResult,
    calendar: CalendarResult,
    a: Assumptions,
    *,
    copper: str,
    key: str,
    label: str,
    name: str = "calendar",
    at_least: bool = False,
    calendar_assumed: bool = True,
) -> CopperLead:
    """Copper treatments avoided = calendar − model copper treatments; % = −avoided / calendar;
    kg Cu/ha = avoided × `cu_kg_ha_treatment`."""
    m_cu = _count((p.products for p in model.dates), copper)
    c_cu = _count((p.products for p in calendar.dates), copper)
    avoided = c_cu - m_cu
    pct = _r(-avoided / c_cu * 100) if c_cu else None
    dose = a.cu_kg_ha_treatment
    kg = _r(avoided * dose.value)
    return CopperLead(
        key=key,
        name=name,
        label=label,
        at_least=at_least,
        model_copper=m_cu,
        calendar_copper=c_cu,
        avoided=avoided,
        pct=pct,
        kg_cu_ha=kg,
        formula=(
            f"({name} {c_cu} − model {m_cu} copper treatments) × {_input(dose)} "
            f"= {fmt(kg)} kg Cu/ha"
        ),
        assumed=dose.assumed or calendar_assumed,
    )


def kpis(
    model: SeasonResult,
    calendar: CalendarResult,
    voice_ops: int,
    area_ha: float,
    a: Assumptions,
    *,
    copper: str,
    sulphur: str,
    calendar_assumed: bool = True,
) -> KpiSet:
    """The season's figures; `calendar_assumed` says the calendar programme compared with is
    itself an assumption (its intervals are, in the advisor's season config)."""
    agreste = config.AGRESTE_TREATMENTS.value
    m_cu = _count((p.products for p in model.dates), copper)
    m_s = _count((p.products for p in model.dates), sulphur)
    c_cu = _count((p.products for p in calendar.dates), copper)
    c_s = _count((p.products for p in calendar.dates), sulphur)
    cal = config.RAPID_CALENDAR_NAME

    treatments = _r(agreste - model.treatments)
    passes = calendar.passes - model.passes
    pass_cost, product_cost = a.pass_cost_eur_ha, a.product_cost_eur_ha
    eur_ha = _r(passes * pass_cost.value + treatments * product_cost.value)
    diesel = _r(passes * a.diesel_l_ha_pass.value)
    co2 = _r(diesel * a.co2_kg_per_l.value)
    cu = _r((c_cu - m_cu) * a.cu_kg_ha_treatment.value)
    s = _r((c_s - m_s) * a.s_kg_ha_treatment.value)
    saved_per_op = a.form_minutes.value - a.voice_minutes.value
    minutes = _r(voice_ops * saved_per_op)
    area = f"{fmt(area_ha)} ha farm area"
    eur_farm, diesel_farm, co2_farm = _r(eur_ha * area_ha), _r(diesel * area_ha), _r(co2 * area_ha)

    def assumed(*inputs: Assumption, calendar: bool = True) -> bool:
        return (calendar and calendar_assumed) or any(i.assumed for i in inputs)

    money = assumed(pass_cost, product_cost)
    fuel = assumed(a.diesel_l_ha_pass)
    gas = assumed(a.diesel_l_ha_pass, a.co2_kg_per_l)
    rows = (
        Kpi(
            "treatments_avoided",
            "Treatments avoided vs the Agreste average",
            treatments,
            "treatments/season",
            f"Agreste average {fmt(agreste, 1)} − model {model.treatments} treatments "
            f"= {fmt(treatments, 1)} treatments/season",
            digits=1,
            assumed=False,
        ),
        Kpi(
            "passes_avoided",
            f"Passes avoided vs the {cal}",
            passes,
            "passes/season",
            f"{cal} {calendar.passes} − model {model.passes} passes = {passes} passes/season",
            digits=0,
            assumed=assumed(),
        ),
        Kpi(
            "eur_ha",
            "Saved per hectare",
            eur_ha,
            "€/ha",
            f"{passes} passes avoided × {_input(pass_cost)} + {fmt(treatments, 1)} treatments "
            f"avoided × {_input(product_cost)} = {fmt(eur_ha)} €/ha",
            assumed=money,
        ),
        Kpi(
            "eur_farm",
            "Saved on the farm",
            eur_farm,
            "€",
            f"{fmt(eur_ha)} €/ha × {area} = {fmt(eur_farm, FARM_DIGITS)} €",
            digits=FARM_DIGITS,
            assumed=money,
        ),
        Kpi(
            "diesel_l_ha",
            "Diesel saved per hectare",
            diesel,
            "L/ha",
            f"{passes} passes avoided × {_input(a.diesel_l_ha_pass)} = {fmt(diesel)} L/ha",
            assumed=fuel,
        ),
        Kpi(
            "diesel_l_farm",
            "Diesel saved on the farm",
            diesel_farm,
            "L",
            f"{fmt(diesel)} L/ha × {area} = {fmt(diesel_farm, FARM_DIGITS)} L",
            digits=FARM_DIGITS,
            assumed=fuel,
        ),
        Kpi(
            "co2_kg_ha",
            "CO₂e avoided per hectare",
            co2,
            "kg CO₂e/ha",
            f"{fmt(diesel)} L/ha diesel × {_input(a.co2_kg_per_l)} = {fmt(co2)} kg CO₂e/ha",
            assumed=gas,
        ),
        Kpi(
            "co2_kg_farm",
            "CO₂e avoided on the farm",
            co2_farm,
            "kg CO₂e",
            f"{fmt(co2)} kg CO₂e/ha × {area} = {fmt(co2_farm, FARM_DIGITS)} kg CO₂e",
            digits=FARM_DIGITS,
            assumed=gas,
        ),
        Kpi(
            "cu_kg_ha",
            f"Copper saved vs the {cal}",
            cu,
            "kg Cu/ha",
            f"({cal} {c_cu} − model {m_cu} copper treatments) × "
            f"{_input(a.cu_kg_ha_treatment)} = {fmt(cu)} kg Cu/ha",
            assumed=assumed(a.cu_kg_ha_treatment),
        ),
        Kpi(
            "s_kg_ha",
            f"Sulphur saved vs the {cal}",
            s,
            "kg S/ha",
            f"({cal} {c_s} − model {m_s} sulphur treatments) × "
            f"{_input(a.s_kg_ha_treatment)} = {fmt(s)} kg S/ha",
            assumed=assumed(a.s_kg_ha_treatment),
        ),
        Kpi(
            "minutes_saved",
            "Typing time saved by voice logging",
            minutes,
            "min",
            f"{voice_ops} voice operations × ({_input(a.form_minutes)} − "
            f"{_input(a.voice_minutes)}) = {fmt(minutes, 1)} min",
            digits=1,
            assumed=assumed(a.form_minutes, a.voice_minutes, calendar=False),
        ),
    )
    return KpiSet(
        model_passes=model.passes,
        model_treatments=model.treatments,
        model_copper=m_cu,
        model_sulphur=m_s,
        calendar_passes=calendar.passes,
        calendar_treatments=calendar.treatments,
        calendar_copper=c_cu,
        calendar_sulphur=c_s,
        agreste_treatments=agreste,
        voice_ops=voice_ops,
        area_ha=area_ha,
        kpis=rows,
    )
