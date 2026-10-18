"""Spray Advisor engine: pure, no DB, clock or I/O. Public seam: `evaluate`."""

from farmcore.apps.advisor.engine.config import AdvisorConfig, SeasonDates, Threshold
from farmcore.apps.advisor.engine.decision import evaluate
from farmcore.apps.advisor.engine.types import (
    Advice,
    HourlyObs,
    ProductRef,
    RuleResult,
    Spray,
)

__all__ = [
    "Advice",
    "AdvisorConfig",
    "HourlyObs",
    "ProductRef",
    "RuleResult",
    "SeasonDates",
    "Spray",
    "Threshold",
    "evaluate",
]
