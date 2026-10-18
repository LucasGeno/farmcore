"""KPI assumptions and labels: every input that is not measured, with value, unit, bounds and
source (no magic numbers in the service).

Defaults are assumptions awaiting agronomist review (`assumed=True`, shown as
"assumed: awaiting agronomist"), except the sourced `diesel_l_ha_pass` (Chambres d'agriculture +
Fédération Cuma Bourgogne), `co2_kg_per_l` (GNR, Arrêté du 26 avril 2017) and
`AGRESTE_TREATMENTS`. The Cu/S doses are assumed. Every page string here is plain text for
farmers and experts: no config keys. The calendar label comes from
`advisor.engine.simulate.CALENDAR_LABEL`. Assumptions are never persisted: the page overrides them
per request from query parameters named like the fields.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict

from farmcore.apps.advisor.engine.config import Threshold
from farmcore.apps.advisor.engine.simulate import CALENDAR_LABEL as CALENDAR_LABEL  # re-export

AGRESTE_TREATMENTS = Threshold[float](
    value=20.1,
    unit="treatments/season",
    source=(
        "Agreste 2019 viticulture practices survey, Bordelais: 20.1 fungicide treatments per "
        "season (a tank-mixed pass counts 2); Agreste Chiffres et Données 2023-4"
    ),
)

REGION = "FRI12"  # station 33394002's NUTS-3 region (Gironde)
YEARS: tuple[int, ...] = (2024, 2025)  # the open station file's seasons

BANNER = "one season, one station, replayed"
RETROSPECTIVE_LABEL = (
    "season retrospective: the whole station season is replayed from the file, whatever the "
    "replay clock; only the voice-operation count is bounded by it"
)
NO_STATION = "KPIs need a station season"
CU_S_LABEL = (
    "Cu / S saved vs the calendar estimate: Agreste publishes no copper/sulphur split, so this "
    "one compares with the calendar programme, not with Agreste"
)
VOICE_LABEL = (
    "voice operations: done spray logs from a voice note (typed chat messages not counted), "
    "started on or before the replay clock, in the season's year"
)

# Lead figure: copper treatments avoided in this season, against two calendars,
# because the number depends on the calendar assumption. The plain one leads ("at least").
LEAD_YEAR = 2024


RAPID_CALENDAR_NAME = "rapid-growth calendar"  # the season's own calendar (`CALENDAR_LABEL`)


def plain_calendar_name(downy_interval_days: float) -> str:
    """The plain calendar's short name, e.g. "plain 10-day calendar"."""
    return f"plain {downy_interval_days:g}-day calendar"


def plain_calendar_label(downy_interval_days: float) -> str:
    """The plain calendar's label, from the season config's `calendar.downy_interval_days`
    (plain text: the template prints it as-is)."""
    return (
        f"plain {downy_interval_days:g}-day copper calendar: the same programme with the usual "
        f"{downy_interval_days:g}-day downy interval all season, no shorter rapid-growth "
        "interval; conservative"
    )


LEAD_LABEL = "Copper treatments (any copper product) avoided"

# The model and the calendar count copper treatments with any copper product, while an
# E-Phy label caps applications per product. The shortlist copper product's vine downy-mildew
# usage allows at most 5 a season (operations seed; tests keep the two in step).
COPPER_TREATMENTS_LABEL = "Copper treatments (any copper product)"
COPPER_LABEL_PRODUCT = "BOUILLIE BORDELAISE RSR DISPERSS NC"
COPPER_LABEL_AMM = "9800474"
COPPER_LABEL_MAX_APPLICATIONS = 5
COPPER_LABEL_SOURCE = "https://ephy.anses.fr/ppp/bouillie-bordelaise-rsr-disperss-nc"
COPPER_CAP_NOTE = (
    f"E-Phy label cap: {COPPER_LABEL_PRODUCT} (AMM {COPPER_LABEL_AMM}) allows max "
    f"{COPPER_LABEL_MAX_APPLICATIONS} applications per season on vine downy mildew; a count above "
    f"{COPPER_LABEL_MAX_APPLICATIONS} means more than one copper product over the season"
)

PENDING = "assumed: awaiting agronomist"


class Assumption(BaseModel):
    """One editable KPI input: value, unit, inclusive bounds and where it comes from;
    `assumed` when no published source backs the value yet."""

    model_config = ConfigDict(frozen=True)

    value: float
    unit: str
    label: str
    source: str
    min: float
    max: float
    assumed: bool = True

    def check(self, value: float) -> float:
        """`value` if finite and within [min, max], else `ValueError`."""
        if not math.isfinite(value) or not self.min <= value <= self.max:
            raise ValueError(f"must be a number in [{self.min:g}, {self.max:g}] {self.unit}")
        return value


def _a(
    value: float, unit: str, label: str, source: str, lo: float, hi: float, *, assumed=True
) -> Any:
    return Assumption(
        value=value, unit=unit, label=label, source=source, min=lo, max=hi, assumed=assumed
    )


class Assumptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    pass_cost_eur_ha: Assumption = _a(
        30, "€/ha/pass", "machine + labour per pass", PENDING, 0, 1000
    )
    product_cost_eur_ha: Assumption = _a(
        12, "€/ha/treatment", "product cost per treatment", PENDING, 0, 1000
    )
    form_minutes: Assumption = _a(
        3.0, "min/operation", "typing one operation into a form", PENDING, 0, 120
    )
    voice_minutes: Assumption = _a(
        0.5, "min/operation", "logging one operation by voice note", PENDING, 0, 120
    )
    diesel_l_ha_pass: Assumption = _a(
        8.7,
        "L/ha/pass",
        "tractor diesel (GNR) per sprayer pass",
        "Chambres d'agriculture + Fédération Cuma Bourgogne 2013–14 vine sprayer measurements: "
        "8.7 L/ha per pass on average (6.4 air-assisted, 10.4 pneumatic with drops)",
        0,
        100,
        assumed=False,
    )
    co2_kg_per_l: Assumption = _a(
        3.17,
        "kg CO₂e/L",
        "CO₂e per litre of GNR (off-road diesel) burnt",
        "Arrêté du 26 avril 2017 (GNR emission factor, 3.17 kg CO₂e/L)",
        0,
        10,
        assumed=False,
    )
    cu_kg_ha_treatment: Assumption = _a(
        0.33, "kg Cu/ha/treatment", "copper metal per copper treatment", PENDING, 0, 10
    )
    s_kg_ha_treatment: Assumption = _a(
        3.0, "kg S/ha/treatment", "sulphur per sulphur treatment", PENDING, 0, 50
    )

    def with_values(self, **values: float) -> Assumptions:
        """A copy with these field values (each checked against its bounds); unknown names are
        a `KeyError`, out-of-bounds or non-finite values a `ValueError` naming the field."""
        update: dict[str, Assumption] = {}
        for name, value in values.items():
            if name not in type(self).model_fields:
                raise KeyError(name)
            current: Assumption = getattr(self, name)
            try:
                checked = current.check(float(value))
            except ValueError as exc:
                raise ValueError(f"{name}: {exc}") from exc
            update[name] = current.model_copy(update={"value": checked})
        return self.model_copy(update=update)

    @classmethod
    def names(cls) -> list[str]:
        return list(cls.model_fields)
