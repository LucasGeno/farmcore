"""Downy mildew primary infections, the Rossi et al. 2008 model (`engine/rossi.py`): its spec and
parameters.

Implemented clean-room from the paper (Rossi, Caffi, Giosuè & Bugiani 2008, Ecological Modelling
212:480-491). Every coefficient below is the paper's, cited with the step it belongs to; the two
FarmCore choices the paper does not make (how a wet hour is read from the inputs, and no zoospore
release at or below 0 °C) are `assumed`. Shown beside the 3-10 rule for comparison: it does not
drive the spray decision until a season's comparison says it should (crop-models spec R4).
"""

from __future__ import annotations

from dataclasses import replace

from farmcore.apps.advisor.engine.mildew import (
    DOWNY,
    HOST,
    PENDING,
    by_name,
    doi,
    hourly,
    param,
)
from farmcore.core.model_spec import Method, ModelInput, ModelSpec, Parameter, Stages, Validation

PAPER = (
    "Rossi et al. 2008, A mechanistic model simulating primary infections of downy mildew in "
    "grapevine, Ecological Modelling 212:480-491"
)
BUCK = "Buck 1981, J. Appl. Meteorol. 20:1527-1532 (saturation vapour pressure over water)"


PAGES = "pp. 482-485"  # section 2, the model's equations


def _paper(step: str) -> str:
    return f"{PAPER}, {step}"


def cited(name: str, value, step: str, unit: str = "", eq: str = "") -> Parameter:
    """A parameter from the paper, with its page range and equation when numbered."""
    return replace(param(name, value, _paper(step), unit), page=f"{PAGES}{', ' + eq if eq else ''}")


PARAMETERS = (
    cited(
        "season_start",
        "01-01",
        "all oospores morphologically mature on 1 January (MMO = 1); northern hemisphere",
        "MM-DD",
    ),
    cited(
        "ht_poly",
        (1330.1, -116.19, 2.6256),
        "hydro-thermal time HT = Σ M / (a + bT + cT²) per hour, 0 at T ≤ 0 °C",
        "h",
        eq="Eq. 3",
    ),
    cited(
        "moist_vpd_max_hpa",
        4.5,
        "moist hour M = 1 when rain > 0 mm or VPD ≤ 4.5 hPa (VPD after Buck 1981)",
        "hPa",
        eq="Eq. 3",
    ),
    param("vapour_pressure", (6.1121, 17.502, 240.97), BUCK + "; e = a·exp(bT / (c + T))", "hPa"),
    cited(
        "dor_gompertz",
        (15.891, 0.653),
        "dormancy breaking DOR = exp(-a·exp(-b·(HT + 1)))",
        eq="Eq. 2",
    ),
    cited(
        "pis_ht",
        (1.3, 8.6),
        "primary inoculum season from HT 1.3 to 8.6 (3 % and 97 % of the dose)",
    ),
    cited(
        "event_rain_min_mm",
        0.2,
        "a wet event, and an oospore cohort, starts with ≥ 0.2 mm rain in its first hour",
        "mm",
    ),
    cited(
        "sporangia_poly",
        (5.67, -0.47, 0.01),
        "sporangia survive SUS = Σ 1 / (24·(a + bX + cX²)), X = T·(1 - RH/100), to 1",
        "d",
    ),
    cited(
        "release_exp",
        (-1.022, 19.634),
        "zoospores released when the wet hours WD ≥ exp((a + b) / TWD), the grouping as "
        "printed; TWD the mean T of the wet period",
        "h",
    ),
    cited(
        "dispersal_rain_min_mm",
        0.2,
        "zoospores dispersed by any rain ≥ 0.2 mm while they survive",
        "mm",
    ),
    cited(
        "infection_degree_hours",
        60.0,
        "infection when WD·TWD ≥ 60 °C·h after dispersal (Blaeser & Weltzien 1979)",
        "°C·h",
    ),
    cited(
        "incubation_poly",
        ((45.1, -3.45, 0.073), (59.9, -4.55, 0.095)),
        "incubation INC = Σ 1 / (24·(a + bT + cT²)) to 1, lower and upper bound",
        "d",
        eq="Eqs. 8-9",
    ),
    param(
        "wet_minutes_min",
        30.0,
        "assumption: an hour is wet with at least 30 wet minutes (sensor, or the relative-humidity "
        "estimate of core/agro leaf_wetness) or ≥ 0.2 mm rain; the paper used measured 0/1 "
        "wetness; " + PENDING,
        "min",
        assumed=True,
    ),
    param(
        "release_tmin_c",
        0.0,
        "assumption: no zoospore release while the wet period's mean T is at or below 0 °C (the "
        "printed release rule has no lower bound there); " + PENDING,
        "°C",
        assumed=True,
    ),
)
P = by_name(PARAMETERS)

VALIDATION = Validation(
    "unvalidated",
    "evaluated by its authors on 43 epidemics in Emilia-Romagna 1995-2006; FarmCore's "
    "implementation is checked against the paper's rules and the viticolR oracle on one synthetic "
    "season only, not against observed disease records",
)
PROVENANCE = (
    "Implemented in FarmCore from the paper; no third-party model code. Golden numbers from "
    "viticolR 0.0.4 (GPL, run out of process) on a synthetic season: tests/fixtures/oracles/."
)

SPEC = ModelSpec(
    key="advisor.downy_rossi",
    title="Downy mildew primary infections (Rossi 2008 model)",
    method=Method("Rossi et al. 2008 mechanistic primary infections, hourly", PAPER, doi(PAPER)),
    host=HOST,
    pests=(DOWNY,),
    stages=Stages(0, None, "from 1 January (oospore maturation); infections need leaves"),
    inputs=(
        hourly("air temperature", "°C"),
        hourly("relative humidity", "%"),
        hourly("rain", "mm"),
        ModelInput("leaf wetness, sensor (wins when present)", "min", "hourly", "measured"),
        ModelInput("leaf wetness from relative humidity (no sensor)", "min", "hourly", "estimated"),
    ),
    parameters=PARAMETERS,
    validation=VALIDATION,
    provenance=PROVENANCE,
)
