"""Powdery mildew: the Gubler–Thomas risk index and the sulphur interval it sets.

The index runs on local calendar days from bloom (`powdery_start`) to bunch closure
(`powdery_end`), on hourly T only. A day counts once it is complete (its 23:00 local row is at or
before `now`), so neither hours after `now` nor the running day move the index. A day
"qualifies" with ≥ `gt_min_hours` consecutive hourly rows in [`gt_band_min_c`, `gt_band_max_c`];
it is "hot" with any hour ≥ `gt_hot_c`. A missing row or T breaks a run.

The series must cover bloom → now: a day with missing rows is still counted (non-qualifying, so
it resets or decrements the index) and only surfaces as `incomplete_days`. A caller loading less
history gets a silently lower index.

Before the index first reaches `gt_onset` a qualifying day adds `gt_step_pre_onset` and any other
day resets it to 0. From onset on (latched: the regime stays even if the index falls back) the
day deltas are `gt_delta_qualify`, `gt_delta_qualify_hot`, `gt_delta_fail`, `gt_delta_fail_hot`.
The index stays within [`gt_index_min`, `gt_index_max`].

Sulphur interval: index > `gt_moderate_max` → high, > `gt_low_max` → moderate, else low (gaps
between the published bands map up).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import window
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.types import (
    EXPIRED,
    REPLAY_FORECAST,
    Advice,
    HourlyObs,
    RuleResult,
)

if TYPE_CHECKING:
    from farmcore.apps.advisor.engine.decision import EvalContext

RULE = "powdery_gubler_thomas"
PASS_RULE = "powdery_pass"
THRESHOLDS = (
    "powdery_start",
    "powdery_end",
    "gt_band_min_c",
    "gt_band_max_c",
    "gt_min_hours",
    "gt_hot_c",
    "gt_onset",
    "gt_step_pre_onset",
    "gt_delta_qualify",
    "gt_delta_qualify_hot",
    "gt_delta_fail",
    "gt_delta_fail_hot",
    "gt_index_min",
    "gt_index_max",
    "gt_low_max",
    "gt_moderate_max",
    "gt_interval_low_days",
    "gt_interval_moderate_days",
    "gt_interval_high_days",
)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class GTDay:
    day: date
    qualifies: bool
    hot: bool
    index: int  # after this day


@dataclass(frozen=True, slots=True)
class GTState:
    index: int
    in_period: bool  # now's local date within [bloom, bunch closure]
    onset_day: date | None
    days: tuple[GTDay, ...]
    incomplete_days: int  # counted days with missing rows or T


def _local_hour(day: date, hour: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=tz).astimezone(UTC)


def _day_flags(rows: list[HourlyObs], config: AdvisorConfig) -> tuple[bool, bool]:
    lo, hi = config.gt_band_min_c.value, config.gt_band_max_c.value
    run = best = 0
    prev: datetime | None = None
    for obs in rows:
        in_band = obs.t_c is not None and lo <= obs.t_c <= hi
        contiguous = prev is not None and obs.ts - prev == HOUR
        run = (run + 1 if contiguous else 1) if in_band else 0
        best = max(best, run)
        prev = obs.ts if in_band else None
    hot = any(o.t_c is not None and o.t_c >= config.gt_hot_c.value for o in rows)
    return best >= config.gt_min_hours.value, hot


def gubler_thomas(past: Sequence[HourlyObs], config: AdvisorConfig, now: datetime) -> GTState:
    tz = ZoneInfo(config.local_tz)
    start, end = config.powdery_start.value, config.powdery_end.value
    today = now.astimezone(tz).date()
    by_day: dict[date, list[HourlyObs]] = defaultdict(list)
    for obs in sorted((o for o in past if o.ts <= now), key=lambda o: o.ts):
        by_day[obs.ts.astimezone(tz).date()].append(obs)

    index = 0
    onset_day: date | None = None
    days: list[GTDay] = []
    incomplete = 0
    day = start
    while day <= end and _local_hour(day, 23, tz) <= now:
        rows = by_day.get(day, [])
        expected = int((_local_hour(day + DAY, 0, tz) - _local_hour(day, 0, tz)) / HOUR)
        if len({o.ts for o in rows if o.t_c is not None}) < expected:
            incomplete += 1
        qualifies, hot = _day_flags(rows, config)
        if onset_day is None:
            index = index + config.gt_step_pre_onset.value if qualifies else 0
        elif qualifies:
            index += (config.gt_delta_qualify_hot if hot else config.gt_delta_qualify).value
        else:
            index += (config.gt_delta_fail_hot if hot else config.gt_delta_fail).value
        index = max(config.gt_index_min.value, min(config.gt_index_max.value, index))
        if onset_day is None and index >= config.gt_onset.value:
            onset_day = day
        days.append(GTDay(day, qualifies, hot, index))
        day += DAY
    return GTState(index, start <= today <= end, onset_day, tuple(days), incomplete)


def risk_level(index: int, config: AdvisorConfig) -> str:
    if index > config.gt_moderate_max.value:
        return "high"
    if index > config.gt_low_max.value:
        return "moderate"
    return "low"


def sulphur_interval_days(index: int, config: AdvisorConfig) -> float:
    level = risk_level(index, config)
    return getattr(config, f"gt_interval_{level}_days").value


def explain(state: GTState, config: AdvisorConfig, extra: dict | None = None) -> RuleResult:
    incomplete = state.in_period and bool(state.incomplete_days)
    level = None if incomplete else risk_level(state.index, config)
    inputs = {
        "index": state.index,
        "risk": level,
        "interval_days": None if incomplete else sulphur_interval_days(state.index, config),
        "in_period": state.in_period,
        "onset_day": state.onset_day,
        "days_counted": len(state.days),
        "last_day": state.days[-1].day if state.days else None,
        "incomplete_days": state.incomplete_days,
        **(extra or {}),
    }
    verdict = (
        ("not evaluable" if incomplete else f"{level} pressure")
        if state.in_period
        else "outside powdery period"
    )
    note = "temperature-only risk phase; hourly 2 m air T, not canopy T"
    return config.explain(RULE, THRESHOLDS, inputs, verdict, note)


@dataclass(frozen=True, slots=True)
class PowderyCover:
    """The powdery cover inside the period: the latest powdery spray lasts the GT interval."""

    spray_ts: datetime | None
    until: datetime | None
    interval_days: float
    washed_off: bool
    expired: bool
    rain_required: bool = False

    @property
    def ended(self) -> bool:
        """Never applied, washed off or expired."""
        return self.spray_ts is None or self.washed_off or self.expired


def period_cover(ctx: EvalContext, state: GTState) -> tuple[PowderyCover, RuleResult]:
    """In the period the sulphur interval follows Gubler–Thomas (not `protection_*_days` and its
    rapid-growth cap); wash-off still follows `protection.py` (`washoff_sulphur_mm`)."""
    config = ctx.config
    base = ctx.covers["powdery"]
    level = risk_level(state.index, config)
    interval = sulphur_interval_days(state.index, config)
    names = (*base.reason.threshold, f"gt_interval_{level}_days")
    note = "in the powdery period the sulphur interval follows the Gubler–Thomas index"
    if base.spray is None:
        cover = PowderyCover(None, None, interval, False, False)
        verdict = "no cover"
    else:
        until = base.spray.ts + timedelta(days=interval)
        expired = ctx.now >= until
        cover = PowderyCover(base.spray.ts, until, interval, base.washed_off, expired)
        verdict = "washed off" if base.washed_off else ("expired" if expired else "protected")
    cover = (
        replace(
            cover,
            rain_required=not cover.ended
            and not any(
                p.product_type == "penetrant" and "powdery" in p.targets
                for p in base.spray.products
            ),
        )
        if base.spray is not None
        else cover
    )
    inputs = {**base.reason.inputs, "until": cover.until, "interval_days": interval}
    return cover, config.explain(base.reason.rule, names, inputs, verdict, note)


def sulphur_window(ctx: EvalContext) -> window.WindowScan:
    """The spray window with the sulphur temperature limit; the reason names it when it bites."""
    scan = window.spray_window(ctx.series, ctx.config, ctx.now, sulphur=True)
    if "sulphur_temp" not in scan.reason.inputs["closed_by"]:
        return scan
    limit = ctx.config.window_sulphur_tmax_c.value
    note = f"sulphur blocked: T ≥ {limit:g} °C (window_sulphur_tmax_c); {scan.reason.note}"
    return replace(scan, reason=replace(scan.reason, note=note))


def _replace_reason(advice: Advice, new: RuleResult) -> None:
    for i, old in enumerate(advice.reasons):
        if old.rule == new.rule:
            advice.reasons[i] = new
            return
    advice.reasons.append(new)


def rule(ctx: EvalContext) -> None:
    """Extra rule: the Gubler–Thomas reason and, in the period, the powdery cover and the
    sulphur-only pass. Shares `ctx.state["powdery"]` (GTState) and `["powdery_cover"]`."""
    config, advice = ctx.config, ctx.advice
    state = gubler_thomas(ctx.past, config, ctx.now)
    ctx.state["powdery"] = state
    advice.reasons.append(explain(state, config))
    if state.in_period and state.incomplete_days:
        advice.assessment_status = "limited"
        advice.flags.add("data_gap")
        return
    if not state.in_period:
        return

    cover, cover_reason = period_cover(ctx, state)
    ctx.state["powdery_cover"] = cover
    _replace_reason(advice, cover_reason)
    if not ctx.covers["downy"].expired:
        advice.flags.discard(EXPIRED)
    if cover.expired:
        advice.flags.add(EXPIRED)

    due = cover.ended and not ctx.downy_due
    if not cover.ended:
        verdict = "covered"
    elif ctx.downy_due:
        verdict = "sulphur due; no sulphur-only pass, tankmix decides (downy pass due)"
    else:
        verdict = "sulphur due"
    inputs = {
        "in_period": True,
        "cover_until": cover.until,
        "never_applied": cover.spray_ts is None,
        "downy_due": ctx.downy_due,
        # The downy cover; on a sulphur-only spray/wait it is also `advice.protected_until`.
        "downy_protected_until": advice.protected_until,
    }
    advice.reasons.append(
        config.explain(PASS_RULE, ("powdery_start", "powdery_end"), inputs, verdict)
    )
    if not due:
        return

    win = sulphur_window(ctx)
    advice.reasons.append(win.reason)
    if win.used_future:
        advice.flags.add(REPLAY_FORECAST)
    advice.decision = "spray" if win.open_now else "wait"
    advice.next_window = win.next_window
    advice.products_recommended = [config.default_powdery_product]
    advice.passes, advice.treatments = 1, 1
