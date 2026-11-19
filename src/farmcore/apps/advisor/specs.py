"""The `ModelSpec` of each crop-protection model (`core/model_spec.py`).

Parameters are read from the engine's own config (`engine/config.py`): value, unit, source and
`assumed` flag, so a spec cannot disagree with the rules it describes. Season dates are set per
region and year, so their spec value is None. No citation is added here that the config does
not already carry; DOIs are left empty where the config names none.

Validation is honest: the models have only been replayed on one farm's seasons against a
calendar programme, so every model is `unvalidated`.
"""

from __future__ import annotations

from farmcore.apps.advisor.engine import downy, incubation, powdery, window
from farmcore.apps.advisor.engine.config import _SRC_GT as GT_SOURCE  # the index's citation
from farmcore.apps.advisor.engine.config import DEFAULT_REGION, GOIDANICH_TABLE, AdvisorConfig
from farmcore.core.model_spec import (
    Method,
    ModelInput,
    ModelSpec,
    Parameter,
    Stages,
    Validation,
)

HOST = "VITVI"  # Vitis vinifera
DOWNY = "PLASVI"  # Plasmopara viticola
POWDERY = "UNCINE"  # Erysiphe necator

VALIDATION = Validation(
    "unvalidated",
    "replayed on the 2024 and 2025 seasons of one farm against a calendar programme; "
    "not yet checked against observed disease records or other regions",
)
PROVENANCE = (
    "Implemented in FarmCore from the published rules cited per parameter; no third-party model "
    "code. No golden fixtures from other implementations yet."
)

# Any year gives the same non-date values; dates are per region and year (value None below).
_CONFIG = AdvisorConfig.for_region_year(DEFAULT_REGION, 2025)


def _parameters(names: tuple[str, ...]) -> tuple[Parameter, ...]:
    out = []
    for name in names:
        t = _CONFIG.threshold(name)
        dated = t.unit == "date"
        out.append(
            Parameter(
                name=name,
                value=None if dated else t.value,
                unit=t.unit,
                source=t.source,
                status="assumed" if t.assumed else "sourced",
                region="per region and year" if dated else "",
            )
        )
    return tuple(out)


def _hourly(variable: str, unit: str, origin: str = "measured") -> ModelInput:
    return ModelInput(variable, unit, "hourly", origin)


_SEASON = Stages(13, None, "from the season gate, shoots ≥ 10 cm (BBCH 13–15); no end stage")

DOWNY_3_10 = ModelSpec(
    key="advisor.downy_3_10",
    title="Downy mildew primary infection (3-10 rule)",
    method=Method("3-10 rule on rolling hourly windows", _CONFIG.downy_rain_min_mm.source),
    host=HOST,
    pests=(DOWNY,),
    stages=_SEASON,
    inputs=(_hourly("rain", "mm"), _hourly("air temperature", "°C")),
    parameters=_parameters(("season_gate", *downy.THRESHOLDS)),
    validation=VALIDATION,
    provenance=PROVENANCE,
)

DOWNY_INCUBATION = ModelSpec(
    key="advisor.downy_incubation",
    title="Downy mildew incubation (Goidanich table)",
    method=Method("Goidanich incubation table, accumulated hourly", GOIDANICH_TABLE.source),
    host=HOST,
    pests=(DOWNY,),
    stages=Stages(13, None, "after each 3-10 infection event; no end stage"),
    inputs=(_hourly("air temperature", "°C"), _hourly("relative humidity", "%")),
    parameters=(
        Parameter(
            name="incub_table",
            value=f"{len(GOIDANICH_TABLE.rows)} rows, {GOIDANICH_TABLE.rows[0][0]:g}–"
            f"{GOIDANICH_TABLE.rows[-1][0]:g} °C",
            unit="%/day",
            source=GOIDANICH_TABLE.source,
            status="assumed",
            note=GOIDANICH_TABLE.note,
        ),
        *_parameters(incubation.THRESHOLDS),
    ),
    validation=VALIDATION,
    provenance=PROVENANCE,
)

POWDERY_GUBLER_THOMAS = ModelSpec(
    key="advisor.powdery_gubler_thomas",
    title="Powdery mildew risk index (Gubler–Thomas)",
    method=Method("Gubler–Thomas risk index on local days", GT_SOURCE),
    host=HOST,
    pests=(POWDERY,),
    stages=Stages(61, None, "from bloom (BBCH 61) to bloom + 28 d (berry resistance)"),
    inputs=(_hourly("air temperature", "°C"),),
    parameters=_parameters(powdery.THRESHOLDS),
    validation=VALIDATION,
    provenance=PROVENANCE,
)

SPRAY_DECISION = ModelSpec(
    key="advisor.spray_decision",
    title="Spray decision (cover, wash-off, spray window, tank-mix)",
    method=Method(
        "FarmCore decision rules: an uncovered infection event and an open spray window",
        "FarmCore crop-protection decision rules; each parameter cites its own source",
    ),
    host=HOST,
    pests=(DOWNY, POWDERY),
    stages=_SEASON,
    inputs=(
        _hourly("rain", "mm"),
        _hourly("rain (forecast)", "mm", "estimated"),
        _hourly("wind speed (forecast)", "km/h", "estimated"),
        _hourly("air temperature (forecast)", "°C", "estimated"),
    ),
    parameters=_parameters(
        (
            "protection_contact_days",
            "protection_penetrant_days",
            "protection_rapid_growth_days",
            "rapid_growth_start",
            "rapid_growth_end",
            "washoff_copper_mm",
            "washoff_sulphur_mm",
            *window.THRESHOLDS,
            "tankmix_cover_ending_days",
        )
    ),
    validation=VALIDATION,
    provenance=PROVENANCE,
)

# Ordered as the About page lists them.
SPECS = (DOWNY_3_10, DOWNY_INCUBATION, POWDERY_GUBLER_THOMAS, SPRAY_DECISION)
