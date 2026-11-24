"""Crop protection's typed result in the shared assessment envelope (`core/assessments.py`).

`SprayResult` is what one evaluation of a parcel concluded: the spray / wait / protected / none
decision with its explained rules, recommended products and windows. Its evidence (status,
parameter version, input snapshot) lives on the envelope beside it. The `advice` row keeps the
public representation every reader uses today; both are written from one engine result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from farmcore.apps.advisor.engine.types import Decision, Disease, Flag, ProductType
from farmcore.core.manifest import AssessmentKind

KIND = AssessmentKind(
    "advisor.spray",
    1,
    "farmcore.apps.advisor.assessment:SprayResult",
    models=tuple(
        f"farmcore.apps.advisor.specs:{name}"
        for name in ("DOWNY_3_10", "DOWNY_INCUBATION", "POWDERY_GUBLER_THOMAS", "SPRAY_DECISION")
    ),
    # The scope that has always read stored advice: every grant and token that holds it keeps
    # exactly its access, and nothing migrates.
    read_scope="advice:read",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Reason(_Strict):
    rule: str = Field(min_length=1)
    inputs: dict[str, Any]
    threshold: dict[str, Any]
    verdict: str
    source: str
    note: str = ""


class Product(_Strict):
    name: str = Field(min_length=1)
    product_type: ProductType
    targets: tuple[Disease, ...]


class SprayResult(_Strict):
    decision: Decision
    headline: str = Field(min_length=1)
    reasons: tuple[Reason, ...]
    products: tuple[Product, ...]
    next_window: tuple[datetime, datetime] | None
    protected_until: datetime | None
    # The service's flags beside the engine's (additive: every saved v1 result still reads).
    flags: tuple[Flag | Literal["spray_without_product", "weather_gap_filled"], ...]
    mm_since_spray: float | None
    incubation_pct: float | None
    passes: int = Field(ge=0)
    treatments: int = Field(ge=0)
