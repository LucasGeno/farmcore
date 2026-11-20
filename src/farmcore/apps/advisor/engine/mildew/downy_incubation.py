"""Downy mildew incubation, the Goidanich table (`engine/incubation.py`): its spec, table and
parameters."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from farmcore.apps.advisor.engine.mildew import (
    DOWNY,
    HOST,
    PENDING,
    PROVENANCE,
    VALIDATION,
    by_name,
    doi,
    hourly,
    param,
)
from farmcore.core.model_spec import Method, ModelSpec, Parameter, Stages

SOURCE = (
    "Goidanich et al. 1957 table via VineSens (Pérez-Expósito et al. 2017, Sensors 17:465, "
    "Table 1, daily rates; FarmCore accumulates them hourly)"
)


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
    source=SOURCE,
    note=(
        "assumed: dry 12.0 °C cell set to 0 (source cell ambiguous); dry 16.5 °C = 9.3 (source "
        "prints 6.3, a typo given its neighbours); 25 °C and above constant. Table form, not the "
        "Orlandini continuous equation (upgrade path: add its parameters here)"
    ),
)

PARAMETERS = (
    Parameter(
        name="incub_table",
        value=f"{len(GOIDANICH_TABLE.rows)} rows, {GOIDANICH_TABLE.rows[0][0]:g}–"
        f"{GOIDANICH_TABLE.rows[-1][0]:g} °C",
        unit="%/day",
        source=SOURCE,
        doi=doi(SOURCE),
        status="assumed",
        note=GOIDANICH_TABLE.note,
    ),
    param(
        "incub_tmin",
        11.0,
        "assumption: no incubation progress at or below 11 °C, linear ramp to the 12.0 °C row "
        "(Charentes table starts at 12 °C; nothing below ~10–11 °C); " + PENDING,
        "°C",
        assumed=True,
    ),
    param(
        "incub_humid_rh_hourly",
        90.0,
        "assumption: hourly RH ≥ 90 % as a leaf-wetness proxy selects the humid column; " + PENDING,
        "%",
        assumed=True,
    ),
    param(
        "incub_alert_pct",
        50.0,
        "Montenegro DSS alert at GI ≥ 50 % (Racković et al. 2026, Sensors 26:2843, PMC13165835)",
        "%",
    ),
    param(
        "incub_symptoms_pct",
        100.0,
        SOURCE + "; GI ≥ 100 % = incubation complete, symptoms expected",
        "%",
    ),
)
P = by_name(PARAMETERS)

SPEC = ModelSpec(
    key="advisor.downy_incubation",
    title="Downy mildew incubation (Goidanich table)",
    method=Method("Goidanich incubation table, accumulated hourly", SOURCE, doi(SOURCE)),
    host=HOST,
    pests=(DOWNY,),
    stages=Stages(13, None, "after each 3-10 infection event; no end stage"),
    inputs=(hourly("air temperature", "°C"), hourly("relative humidity", "%")),
    parameters=PARAMETERS,
    validation=VALIDATION,
    provenance=PROVENANCE,
)
