"""Powdery mildew, the Gubler–Thomas risk index (`engine/powdery.py`): its spec and parameters.

The index runs from bloom to bloom + `POWDERY_RESISTANCE_DAYS`; both dates are set per region
and year in `config.py`.
"""

from __future__ import annotations

from farmcore.apps.advisor.engine.mildew import (
    HOST,
    PENDING,
    POWDERY,
    PROVENANCE,
    REPLAYED,
    by_name,
    dated,
    hourly,
    param,
)
from farmcore.core.model_spec import Method, ModelSpec, Stages, Validation

SOURCE = (
    "UC Davis powdery mildew risk index (Gubler–Thomas), UC IPM "
    "ipm.ucanr.edu/agriculture/grape/powdery-mildew/; Thomas, Gubler & Leavitt 1994, "
    "Phytopathology 84:1070, abstract"
)
BLOOM_SOURCE = "assumption: bloom (BBCH 61) dated per region and year; " + PENDING
# Berries resist powdery mildew ~3–4 weeks after bloom, so the Gubler–Thomas period (and the
# sulphur passes it drives) ends at bloom + 28 d, not at bunch closure.
POWDERY_RESISTANCE_DAYS = 28
END_SOURCE = (
    f"bloom + {POWDERY_RESISTANCE_DAYS} d (powdery_resistance_days): berries resistant to "
    "powdery mildew 3–4 weeks after bloom (Gadoury 2003); bloom date " + PENDING
)

PARAMETERS = (
    dated("powdery_start", BLOOM_SOURCE),
    dated("powdery_end", END_SOURCE),
    param("gt_band_min_c", 21.0, SOURCE + " (70 °F)", "°C"),
    param("gt_band_max_c", 29.5, SOURCE + " (85 °F)", "°C"),
    param("gt_min_hours", 6, SOURCE + "; ≥ 6 continuous hours in the band", "h"),
    param(
        "gt_hot_c",
        35.0,
        SOURCE + "; ≥ 35 °C (95 °F) for > 15 min; hourly proxy: any hour ≥ 35 °C",
        "°C",
    ),
    param("gt_onset", 60, SOURCE + "; epidemic onset at 60; before it, resets"),
    param("gt_step_pre_onset", 20, SOURCE + "; +20 per qualifying day"),
    param("gt_delta_qualify", 20, SOURCE + "; after onset, ≥ 6 h in band → +20"),
    param("gt_delta_qualify_hot", 10, SOURCE + "; after onset, both conditions the same day → +10"),
    param("gt_delta_fail", -10, SOURCE + "; after onset, < 6 h in band → −10"),
    param(
        "gt_delta_fail_hot",
        -20,
        "assumption: after onset, < 6 h in band and a ≥ 35 °C hour → −20 (UC IPM reading, "
        "both penalties); " + PENDING,
        assumed=True,
    ),
    param("gt_index_min", 0, SOURCE + "; index bounded 0–100"),
    param("gt_index_max", 100, SOURCE + "; index bounded 0–100"),
    param("gt_low_max", 30, SOURCE + "; 0–30 low; values above (e.g. 35) map up to moderate"),
    param(
        "gt_moderate_max", 50, SOURCE + "; 40–50 moderate; values above (e.g. 55) map up to high"
    ),
    param(
        "gt_interval_low_days",
        18.0,
        "assumption: midpoint of UC IPM sulphur interval 14–21 d at low risk; " + PENDING,
        "d",
        assumed=True,
    ),
    param(
        "gt_interval_moderate_days",
        14.0,
        "assumption: within UC IPM sulphur interval 10–17 d at moderate risk ; " + PENDING,
        "d",
        assumed=True,
    ),
    param("gt_interval_high_days", 7.0, SOURCE + "; sulphur interval 7 d at high risk (≥ 60)", "d"),
)
P = by_name(PARAMETERS)

SPEC = ModelSpec(
    key="advisor.powdery_gubler_thomas",
    title="Powdery mildew risk index (Gubler–Thomas)",
    method=Method("Gubler–Thomas risk index on local days", SOURCE),
    host=HOST,
    pests=(POWDERY,),
    stages=Stages(61, None, "from bloom (BBCH 61) to bloom + 28 d (berry resistance)"),
    inputs=(hourly("air temperature", "°C"),),
    parameters=PARAMETERS,
    validation=Validation(
        "unvalidated",
        REPLAYED + "; the method is published by UC IPM, but this implementation (an hourly "
        "proxy for the 15-minute heat rule, an assumed combined penalty) is not compared with it",
    ),
    provenance=PROVENANCE,
)
