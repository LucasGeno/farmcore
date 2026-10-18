"""`evaluate`: one pure call from an hourly series and past sprays to explained advice.

Decision:

- before the season gate → `none` ("season not started");
- no 3-10 event in the lookback (`downy_event_max_days`) → `none`;
- the latest downy cover holds → `protected` (until, mm since spray);
- an uncovered event (no downy spray at or after it) and the window is open now → `spray`
  (window, products: copper as downy contact by default);
- an uncovered event otherwise → `wait` (next window);
- every event in the lookback was ended by a later cover that no longer holds → `none`: the cover
  ended the event, and a washed-off or expired cover does not revive it (a new crossing does).

Washed off and expired are flags on the latest cover per disease. No future leak: everything but
`window.py` reads only hours and sprays with ts ≤ now.

`extra_rules` (default `EXTRA_RULES`, in this order) run last on an `EvalContext`, append reasons
and may adjust the advice:

- `incubation.rule`: Goidanich progress of each 3-10 event (a risk indicator; never decides);
- `powdery.rule`: Gubler–Thomas index; inside the powdery period (bloom → bunch closure) the
  powdery cover lasts the GT sulphur interval, and with that cover ended (never applied counts)
  and no downy pass due (`protected` included) → the **sulphur-only pass**: `spray` when the
  sulphur window (T < 32 °C) is open now, else `wait`; 1 pass, 1 treatment. Outside the period
  nothing changes (`none` stays `none`);
- `tankmix.rule`: a due downy pass carries sulphur when the powdery cover ends within 2 days (or
  has ended) in the period and the sulphur window starts with copper's; else copper alone.

`passes` / `treatments` describe the recommended pass (1 / number of products), 0 without one.

`protected_until` always means the **downy** cover. The sulphur-only pass may turn a `protected`
advice into `spray` / `wait` and leaves it set: there it says "no downy pass due, copper holds
until …", not that the spray card is protected (also echoed as `downy_protected_until` on the
`powdery_pass` reason). The powdery cover date is the `protection_powdery` reason's `until`.

History: the caller loads the series from the season gate (3-10, incubation) and from bloom
(`powdery_start`, Gubler–Thomas) up to now; days with missing rows count as non-qualifying
(surfaced as `incomplete_days`), so a shorter history gives a silently lower GT index.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import downy, incubation, powdery, tankmix, window
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.gate import gate_start, season_gate
from farmcore.apps.advisor.engine.protection import Cover, latest_cover
from farmcore.apps.advisor.engine.types import (
    DATA_GAP,
    EXPIRED,
    REPLAY_FORECAST,
    WASHED_OFF,
    Advice,
    Flag,
    HourlyObs,
    Spray,
    aware_utc,
)


@dataclass(frozen=True, slots=True)
class EvalContext:
    series: list[HourlyObs]  # sorted, may include hours after now (window only)
    past: list[HourlyObs]  # ts ≤ now
    sprays: list[Spray]  # ts ≤ now
    config: AdvisorConfig
    now: datetime
    covers: dict[str, Cover]
    advice: Advice
    downy_events: list[datetime]  # every 3-10 event from the gate to now
    downy_due: bool  # the core recommends a downy pass (active event, no holding cover)
    # Shared between rules: "downy_window" (core, when a downy pass is due), "powdery" and
    # "powdery_cover" (powdery.rule).
    state: dict[str, Any] = field(default_factory=dict)


ExtraRule = Callable[[EvalContext], None]
EXTRA_RULES: list[ExtraRule] = [incubation.rule, powdery.rule, tankmix.rule]  # order matters


def _event_window(
    past: Sequence[HourlyObs], event: datetime | None, config: AdvisorConfig
) -> tuple[float | None, float | None]:
    """Rain sum and mean temperature of the rows with ts in (event − window, event]; rows with
    a missing value are skipped, not zeroed (as `downy.scan`). (None, None) without an event."""
    if event is None:
        return None, None
    since = event - timedelta(hours=config.downy_window_h.value)
    rows = [o for o in past if since < o.ts <= event]
    rains = [o.rr1_mm for o in rows if o.rr1_mm is not None]
    temps = [o.t_c for o in rows if o.t_c is not None]
    return (
        round(sum(rains), 6) if rains else None,
        sum(temps) / len(temps) if temps else None,
    )


def evaluate(
    series: Sequence[HourlyObs],
    sprays: Sequence[Spray],
    config: AdvisorConfig,
    now: datetime,
    *,
    extra_rules: Sequence[ExtraRule] | None = None,
) -> Advice:
    now = aware_utc(now, "evaluate: `now`")
    started, gate = season_gate(config, now)
    if not started:
        return Advice(decision="none", reasons=[gate])

    ordered = sorted(series, key=lambda o: o.ts)
    past = [o for o in ordered if o.ts <= now]
    done = sorted((s for s in sprays if s.ts <= now), key=lambda s: s.ts)
    flags: set[Flag] = set()

    covers = {d: latest_cover(past, done, config, now, d) for d in ("downy", "powdery")}
    downy_cover = covers["downy"]
    # Card flags describe the covers that drive the decision: downy always, powdery only inside
    # its period (bloom → resistance), so a stale sulphur cover in May does not read "expired".
    local_day = now.astimezone(ZoneInfo(config.local_tz)).date()
    in_powdery = config.powdery_start.value <= local_day <= config.powdery_end.value
    for disease, cover in covers.items():
        if disease == "powdery" and not in_powdery:
            continue
        if cover.washed_off:
            flags.add(WASHED_OFF)
        if cover.expired:
            flags.add(EXPIRED)
        if cover.data_gap:
            flags.add(DATA_GAP)

    scan = downy.scan(past, config, gate_start(config), now)
    if scan.data_gap_hours:
        flags.add(DATA_GAP)
    lookback_start = now - timedelta(days=config.downy_event_max_days.value)
    recent = [e for e in scan.events if e > lookback_start]
    downy_sprays = [s.ts for s in done if any("downy" in p.targets for p in s.products)]
    active = [e for e in recent if not any(ts >= e for ts in downy_sprays)]
    verdict = "risk" if active else ("covered" if recent else "no risk")
    event_at = max(active) if active else (max(recent) if recent else None)
    event_r48, event_t48 = _event_window(past, event_at, config)
    downy_reason = config.explain(
        downy.RULE,
        downy.THRESHOLDS,
        {
            # The infection event's own window first (the card lists inputs in order), then the
            # window at now: `r48_mm` can be below the threshold while the event is still risk.
            "event_at": event_at,
            "event_r48_mm": event_r48,
            "event_t48_c": event_t48,
            "r48_mm": scan.r48_mm,
            "t48_c": scan.t48_c,
            "events": scan.events,
            "recent_events": recent,
            "active_events": active,
            "lookback_start": lookback_start,
            "data_gap_hours": len(scan.data_gap_hours),
        },
        verdict,
        "infection event in lookback, not covered" if active else "",
    )
    reasons = [gate, downy_reason, covers["downy"].reason, covers["powdery"].reason]
    state: dict[str, Any] = {}
    advice = Advice(
        decision="none",
        reasons=reasons,
        mm_since_spray=downy_cover.mm_since_spray,
        flags=flags,
    )

    if recent and downy_cover.valid:
        advice.decision = "protected"
        advice.protected_until = downy_cover.until
    elif active:
        products = [config.default_downy_product]
        sulphur = any("powdery" in p.targets for p in products)
        win = window.spray_window(ordered, config, now, sulphur=sulphur)
        state["downy_window"] = win
        reasons.append(win.reason)
        if win.used_future:
            flags.add(REPLAY_FORECAST)
        advice.decision = "spray" if win.open_now else "wait"
        advice.next_window = win.next_window
        advice.products_recommended = products
        advice.passes, advice.treatments = 1, len(products)

    ctx = EvalContext(
        ordered,
        past,
        done,
        config,
        now,
        covers,
        advice,
        scan.events,
        "downy_window" in state,
        state,
    )
    for rule in EXTRA_RULES if extra_rules is None else extra_rules:
        rule(ctx)
    return advice
