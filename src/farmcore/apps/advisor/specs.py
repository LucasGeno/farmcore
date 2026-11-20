"""The `ModelSpec` of each crop-protection model (`core/model_spec.py`).

The mildew models are ModelSpec modules of their own (`engine/mildew/`): each owns its
parameters, and `engine/config.py` reads its defaults from them. They are re-exported here, the
path the assessment kind names. The decision model's parameters are read from the engine's own
config (`engine/config.py`): value, unit, source and `assumed` flag, so the spec cannot disagree
with the rules it describes; they are the engine rules' own `THRESHOLDS`. A test checks that
every engine threshold is in some spec.
"""

from __future__ import annotations

from farmcore.apps.advisor.engine import protection, tankmix, window
from farmcore.apps.advisor.engine.config import DEFAULT_REGION, AdvisorConfig
from farmcore.apps.advisor.engine.mildew import (
    DOWNY,
    HOST,
    POWDERY,
    PROVENANCE,
    VALIDATION,
    doi,
    hourly,
)
from farmcore.apps.advisor.engine.mildew.downy_3_10 import SEASON
from farmcore.apps.advisor.engine.mildew.downy_3_10 import SPEC as DOWNY_3_10
from farmcore.apps.advisor.engine.mildew.downy_incubation import SPEC as DOWNY_INCUBATION
from farmcore.apps.advisor.engine.mildew.powdery_gubler_thomas import (
    SPEC as POWDERY_GUBLER_THOMAS,
)
from farmcore.core.model_spec import Method, ModelSpec, Parameter

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
                doi=doi(t.source),
                status="assumed" if t.assumed else "sourced",
                region="per region and year" if dated else "",
            )
        )
    return tuple(out)


SPRAY_DECISION = ModelSpec(
    key="advisor.spray_decision",
    title="Spray decision (cover, wash-off, spray window, tank-mix)",
    method=Method(
        "FarmCore decision rules: an uncovered infection event and an open spray window",
        "FarmCore crop-protection decision rules; each parameter cites its own source",
    ),
    host=HOST,
    pests=(DOWNY, POWDERY),
    stages=SEASON,
    inputs=(
        hourly("rain", "mm"),
        hourly("rain (forecast)", "mm", "estimated"),
        hourly("wind speed (forecast)", "km/h", "estimated"),
        hourly("air temperature (forecast)", "°C", "estimated"),
    ),
    parameters=_parameters((*protection.THRESHOLDS, *window.THRESHOLDS, *tankmix.THRESHOLDS)),
    validation=VALIDATION,
    provenance=PROVENANCE,
)

# Ordered as the About page lists them.
SPECS = (DOWNY_3_10, DOWNY_INCUBATION, POWDERY_GUBLER_THOMAS, SPRAY_DECISION)
