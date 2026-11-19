"""Engine data shapes: hourly observations in, sprays in, explained advice out.

All timestamps are timezone-aware: `HourlyObs` (core's shared hourly weather shape,
`core/weather/hourly.py`, re-exported here) and `Spray` raise `ValueError` on a naive `ts` and
hold it in UTC. `Spray.products` and `ProductRef.targets` accept any sequence (a list from a log
mapping is fine) and are held as tuples, so the shapes stay frozen and hashable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from farmcore.core.weather.hourly import HourlyObs as HourlyObs
from farmcore.core.weather.hourly import aware_utc as aware_utc
from farmcore.core.weather.hourly import ceil_hour as ceil_hour
from farmcore.core.weather.hourly import floor_hour as floor_hour

Decision = Literal["spray", "wait", "protected", "none"]
ProductType = Literal["contact", "penetrant"]
Disease = Literal["downy", "powdery"]
Flag = Literal["washed_off", "expired", "data_gap", "replay_forecast"]

WASHED_OFF: Flag = "washed_off"
EXPIRED: Flag = "expired"
DATA_GAP: Flag = "data_gap"
REPLAY_FORECAST: Flag = "replay_forecast"


@dataclass(frozen=True, slots=True)
class ProductRef:
    name: str
    product_type: ProductType
    targets: Sequence[Disease]  # held as a tuple

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(self.targets))


@dataclass(frozen=True, slots=True)
class Spray:
    ts: datetime
    products: Sequence[ProductRef]  # held as a tuple

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", aware_utc(self.ts, "Spray.ts"))
        object.__setattr__(self, "products", tuple(self.products))


@dataclass(frozen=True, slots=True)
class RuleResult:
    """One explained rule: what it read, which config values it compared with, what it found."""

    rule: str
    inputs: dict[str, Any]
    threshold: dict[str, Any]
    verdict: str
    source: str
    note: str = ""


@dataclass(slots=True)
class Advice:
    decision: Decision
    reasons: list[RuleResult]
    next_window: tuple[datetime, datetime] | None = None
    # Always the downy cover: also set on a sulphur-only spray/wait when downy is covered.
    protected_until: datetime | None = None
    mm_since_spray: float | None = None
    products_recommended: list[ProductRef] = field(default_factory=list)
    flags: set[Flag] = field(default_factory=set)
    # The recommended pass: 1 pass, one treatment per product (a tank-mix counts 2); 0 without one.
    passes: int = 0
    treatments: int = 0
    assessment_status: Literal["evaluable", "limited", "unavailable"] = "evaluable"

    def reason(self, rule: str) -> RuleResult | None:
        """The first reason produced by `rule`, if any."""
        return next((r for r in self.reasons if r.rule == rule), None)
