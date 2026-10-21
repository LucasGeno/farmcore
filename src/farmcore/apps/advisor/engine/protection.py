"""Spray covers per disease: expiry by product type and phase, wash-off by rain since the spray.

Only the latest spray (ts ≤ now) whose products target a disease is that disease's cover. Each of
its products lasts `protection_<type>_days`, capped at `protection_rapid_growth_days` when sprayed
(local date) inside the rapid-growth phase. Contact products wash off once the rain over rows with
ts ∈ (spray, now] reaches the disease's threshold (copper for downy, sulphur for powdery);
penetrants are rainfast. The cover holds while any of its products is neither expired nor washed
off. Reads only rows and sprays with ts ≤ now.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.types import Disease, HourlyObs, RuleResult, Spray, floor_hour

WASHOFF_KEY: dict[Disease, str] = {"downy": "washoff_copper_mm", "powdery": "washoff_sulphur_mm"}
_COMMON = (
    "protection_contact_days",
    "protection_penetrant_days",
    "protection_rapid_growth_days",
    "rapid_growth_start",
    "rapid_growth_end",
)


def rule_name(disease: Disease) -> str:
    return f"protection_{disease}"


@dataclass(frozen=True, slots=True)
class Cover:
    disease: Disease
    spray: Spray | None
    until: datetime | None
    mm_since_spray: float | None
    washed_off: bool
    expired: bool
    data_gap: bool
    reason: RuleResult
    rain_required: bool = False

    @property
    def valid(self) -> bool:
        return self.spray is not None and not (self.washed_off or self.expired)


def rain_since(past: Sequence[HourlyObs], start: datetime, now: datetime) -> tuple[float, bool]:
    """(mm over rows with ts in (start, now], missing rows or values present)."""
    by_hour: dict[datetime, set[float | None]] = {}
    for row in past:
        if start < row.ts <= now:
            by_hour.setdefault(row.ts.astimezone(UTC), set()).add(row.rr1_mm)
    # Only unambiguous readings on the expected UTC grid establish known rain.
    # Repeated identical readings count once; conflicting/off-grid rows are gaps.
    known = {
        ts: next(iter(values))
        for ts, values in by_hour.items()
        if ts == floor_hour(ts) and len(values) == 1 and None not in values
    }
    total = round(sum(known.values()), 6)
    expected = max(
        0,
        int(
            (floor_hour(now.astimezone(UTC)) - floor_hour(start.astimezone(UTC)))
            / timedelta(hours=1)
        ),
    )
    gap = len(known) < expected or len(known) != len(by_hour)
    return total, gap


def _in_rapid_growth(spray: Spray, config: AdvisorConfig) -> bool:
    day = spray.ts.astimezone(ZoneInfo(config.local_tz)).date()
    return config.rapid_growth_start.value <= day <= config.rapid_growth_end.value


def latest_cover(
    past: Sequence[HourlyObs],
    sprays: Sequence[Spray],
    config: AdvisorConfig,
    now: datetime,
    disease: Disease,
) -> Cover:
    names = (*_COMMON, WASHOFF_KEY[disease])
    rule = rule_name(disease)
    candidates = [
        s for s in sprays if s.ts <= now and any(disease in p.targets for p in s.products)
    ]
    if not candidates:
        reason = config.explain(rule, names, {"spray_ts": None}, "no cover")
        return Cover(disease, None, None, None, False, False, False, reason)

    spray = max(candidates, key=lambda s: s.ts)
    products = [p for p in spray.products if disease in p.targets]
    rapid = _in_rapid_growth(spray, config)
    mm, gap = rain_since(past, spray.ts, now)
    washoff_mm = getattr(config, WASHOFF_KEY[disease]).value

    untils: list[datetime] = []
    washed: list[bool] = []
    for product in products:
        days = (
            config.protection_contact_days.value
            if product.product_type == "contact"
            else config.protection_penetrant_days.value
        )
        if rapid:
            days = min(days, config.protection_rapid_growth_days.value)
        untils.append(spray.ts + timedelta(days=days))
        washed.append(product.product_type == "contact" and mm >= washoff_mm)

    holding = [u for u, w in zip(untils, washed, strict=True) if not w and now < u]
    if holding:
        until, is_washed, is_expired = max(holding), False, False
        verdict = "protected"
    else:
        until = max(untils)
        is_washed = any(washed)
        is_expired = any(now >= u for u in untils)
        verdict = "washed off" if is_washed else "expired"

    inputs = {
        "spray_ts": spray.ts,
        "products": [p.name for p in products],
        "product_types": [p.product_type for p in products],
        "rapid_growth": rapid,
        "until": until,
        "mm_since_spray": mm,
        "rain_data_gap": gap,
    }
    reason = config.explain(rule, names, inputs, verdict)
    rain_required = bool(holding) and not any(
        p.product_type == "penetrant" and now < u for p, u in zip(products, untils, strict=True)
    )
    return Cover(disease, spray, until, mm, is_washed, is_expired, gap, reason, rain_required)
