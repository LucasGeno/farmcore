"""Engine data shapes: hourly observations in, sprays in, explained advice out.

All timestamps are timezone-aware: `HourlyObs` and `Spray` raise `ValueError` on a naive `ts` and
hold it in UTC. `HourlyObs.rr1_mm` is the rain in the hour ending at `ts` (Météo-France RR1);
`None` means the value is missing, never zero. `Spray.products` and `ProductRef.targets` accept
any sequence (a list from a log mapping is fine) and are held as tuples, so the shapes stay
frozen and hashable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

Decision = Literal["spray", "wait", "protected", "none"]
ProductType = Literal["contact", "penetrant"]
Disease = Literal["downy", "powdery"]
Flag = Literal["washed_off", "expired", "data_gap", "replay_forecast"]

WASHED_OFF: Flag = "washed_off"
EXPIRED: Flag = "expired"
DATA_GAP: Flag = "data_gap"
REPLAY_FORECAST: Flag = "replay_forecast"


def aware_utc(ts: datetime, what: str) -> datetime:
    """`ts` in UTC; a naive `ts` is a `ValueError` naming `what`."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got naive {ts.isoformat()}")
    return ts.astimezone(UTC)


def floor_hour(ts: datetime) -> datetime:
    """`ts` floored to the hour (region UTC offsets are whole hours)."""
    return ts.replace(minute=0, second=0, microsecond=0)


def ceil_hour(ts: datetime) -> datetime:
    floor = floor_hour(ts)
    return floor if floor == ts else floor + timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class HourlyObs:
    ts: datetime
    rr1_mm: float | None = None
    t_c: float | None = None
    rh_pct: float | None = None
    ff_kmh: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", aware_utc(self.ts, "HourlyObs.ts"))


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
