"""Tank-mix: one pass, two treatments.

When a downy pass is due and, inside the powdery period, the powdery cover ends within
`tankmix_cover_ending_days` (or has ended, never applied included), the pass carries the default
powdery product (sulphur) next to copper. Sulphur needs the sulphur window (T < 32 °C): it joins
only when its window starts with copper's; otherwise copper goes alone.

The `tankmix` reason reads only the past (cover, period): "tank-mix due" says sulphur is due with
copper, not that it rides. The outcome reads hours after `now`, so it sits on the sulphur
`spray_window` reason's note: "copper + sulphur: 1 pass, 2 treatments" or "copper alone: <cause>"
(the first hour of copper's window that is ≥ 32 °C or has no T, or no window in the horizon).
Runs after `powdery.rule` (reads `ctx.state["powdery"]`, `["powdery_cover"]`, and
`["downy_window"]` from the core decision).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from farmcore.apps.advisor.engine import powdery, window
from farmcore.apps.advisor.engine.types import REPLAY_FORECAST

if TYPE_CHECKING:
    from farmcore.apps.advisor.engine.decision import EvalContext

RULE = "tankmix"
THRESHOLDS = ("tankmix_cover_ending_days", "powdery_start", "powdery_end")
HOUR = timedelta(hours=1)


def _blocked_cause(ctx: EvalContext, copper: window.WindowScan) -> str:
    """Why sulphur does not start with copper. The sulphur window is copper's plus T < limit, so
    the cause is the first hour of copper's window that fails the sulphur temperature."""
    if copper.next_window is None:
        return "no spray window within the horizon, sulphur not added"
    limit = ctx.config.window_sulphur_tmax_c.value
    rows = {o.ts: o for o in ctx.series}
    h, end = copper.next_window
    while h < end:
        obs = rows.get(h)
        at = f"{h.astimezone(UTC):%Y-%m-%d %H:%M} UTC in copper's window"
        if obs is None or obs.t_c is None:
            return f"sulphur blocked: no T at {at} (window_sulphur_tmax_c)"
        if obs.t_c >= limit:
            return (
                f"sulphur blocked: T ≥ {limit:g} °C (window_sulphur_tmax_c), {obs.t_c:g} °C at {at}"
            )
        h += HOUR
    return "sulphur window does not start with copper's"


def rule(ctx: EvalContext) -> None:
    config, advice = ctx.config, ctx.advice
    state: powdery.GTState | None = ctx.state.get("powdery")
    cover: powdery.PowderyCover | None = ctx.state.get("powdery_cover")
    ending = timedelta(days=config.tankmix_cover_ending_days.value)
    inputs = {
        "downy_due": ctx.downy_due,
        "in_period": bool(state and state.in_period),
        "powdery_cover_until": cover.until if cover else None,
    }

    def explain(verdict: str) -> None:
        advice.reasons.append(config.explain(RULE, THRESHOLDS, inputs, verdict))

    if not ctx.downy_due:
        return explain("no downy pass due")
    if state is None or not state.in_period or cover is None:
        return explain("outside powdery period")
    if not (cover.ended or cover.until - ctx.now <= ending):
        return explain("copper only: powdery cover holds")
    explain("tank-mix due: sulphur with copper")

    copper: window.WindowScan = ctx.state["downy_window"]
    sulphur = window.spray_window(ctx.series, config, ctx.now, sulphur=True)
    if sulphur.used_future:
        advice.flags.add(REPLAY_FORECAST)
    joins = (
        sulphur.next_window is not None
        and copper.next_window is not None
        and sulphur.next_window[0] == copper.next_window[0]
    )
    outcome = (
        "copper + sulphur: 1 pass, 2 treatments"
        if joins
        else f"copper alone: {_blocked_cause(ctx, copper)}"
    )
    advice.reasons.append(replace(sulphur.reason, note=f"{outcome}; {sulphur.reason.note}"))
    if not joins:
        return None
    advice.next_window = sulphur.next_window
    advice.products_recommended = [*advice.products_recommended, config.default_powdery_product]
    advice.passes, advice.treatments = 1, len(advice.products_recommended)
    return None
