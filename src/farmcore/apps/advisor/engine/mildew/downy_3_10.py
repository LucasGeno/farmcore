"""Downy mildew primary infection, the 3-10 rule (`engine/downy.py`): its spec and parameters.

The season gate (`engine/gate.py`) opens the rule, so its date is a parameter here too.
"""

from __future__ import annotations

from farmcore.apps.advisor.engine.mildew import (
    DOWNY,
    HOST,
    PENDING,
    PROVENANCE,
    VALIDATION,
    by_name,
    dated,
    doi,
    hourly,
    param,
)
from farmcore.core.model_spec import Method, ModelSpec, Stages

SOURCE = "3-10 rule (Baldacci 1947) via Velasquez-Camacho et al. 2023, Microorganisms 11(1):73"
GATE_SOURCE = (
    "assumption: shoots ≥ 10 cm (BBCH 13–15) dated per region and year; Bordeaux usually "
    "mid–late April (BSV Sud Aquitaine); default, " + PENDING
)
SEASON = Stages(13, None, "from the season gate, shoots ≥ 10 cm (BBCH 13–15); no end stage")

PARAMETERS = (
    dated("season_gate", GATE_SOURCE),
    param("downy_window_h", 48, SOURCE + " (≥ 10 mm in 24–48 h)", "h"),
    param("downy_rain_min_mm", 10.0, SOURCE, "mm"),
    param("downy_temp_min_c", 10.0, SOURCE + " (mean T ≥ 10 °C)", "°C"),
    param(
        "downy_event_max_days",
        3.0,
        "assumption: an uncovered event stays active risk 3 days; " + PENDING,
        "d",
        assumed=True,
    ),
)
P = by_name(PARAMETERS)

SPEC = ModelSpec(
    key="advisor.downy_3_10",
    title="Downy mildew primary infection (3-10 rule)",
    method=Method("3-10 rule on rolling hourly windows", SOURCE, doi(SOURCE)),
    host=HOST,
    pests=(DOWNY,),
    stages=SEASON,
    inputs=(hourly("rain", "mm"), hourly("air temperature", "°C")),
    parameters=PARAMETERS,
    validation=VALIDATION,
    provenance=PROVENANCE,
)
