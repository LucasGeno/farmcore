"""Daily engine form: the Spray Advisor on regional daily data.

Same decision as `decision.evaluate`, with daily rules (thresholds in `config.daily`, labelled
"daily regional data; 2-calendar-day approximation"):

- **3-10 on 2 calendar days**: on local date d, rain over the `daily.downy_days` dates ending at d
  (missing days skipped, not zeroed) ≥ `downy_rain_min_mm` and their mean T ≥
  `downy_temp_min_c`; edge-triggered, re-arms when the rain sum drops below the minimum. An event
  is dated at the end of its day (local midnight). Lookback and cover as in the hourly engine.
  The scan runs through the last complete local date at `now`, so days missing at the end of the
  feed raise `data_gap` inside the lookback (as the hourly engine's tail hours do).
- **Daily window**: day d is open when its mean wind ≤ `daily.window_wind_max_ms`, its rain <
  `daily.window_rain_max_mm`, the next day's rain < `daily.window_rain_next_max_mm` and, for
  sulphur, its Tmax < `window_sulphur_tmax_c`. Missing values close the day. The next window is
  the first open day in the horizon (`window_horizon_h`, in whole days); it opens at
  `calendar.eval_hour_local` (or `now`, today) and ends at the day's end.

- **Daily Goidanich**: each complete day after a 3-10 event adds the table rate at its mean T,
  humid column at daily mean RH ≥ `daily.incub_daily_rh_split` (the table's own definition;
  the hourly engine keeps its own `incub_humid_rh_hourly` proxy). A risk indicator only.
- **Gubler–Thomas** on a synthetic hourly T curve from each day's Tmin/Tmax (derived): a
  half-cosine rise from Tmin at `daily.t_min_hour_local` to Tmax at `daily.t_max_hour_local`
  and a half-cosine fall back towards Tmin over the other hours, using that day's values only.
  Then `powdery.gubler_thomas` as in the hourly engine; sulphur-only pass and tank-mix as in
  `powdery.rule` / `tankmix.rule`, on the daily window, with the same reasons and verdicts
  (`tankmix` "no downy pass due", `powdery_pass` when a downy pass is due, the "sulphur blocked"
  note and the copper-alone cause on copper's day Tmax). The `downy_3_10` reason uses the hourly
  input names (`r48_mm`, `t48_c` over the 2 calendar days); gaps are `data_gap_days`, not hours.
  Like the hourly reason it lists `event_at`, `event_r48_mm`, `event_t48_c` first: the 2 days
  ending at the latest active event (else the latest recent one; None without).

A day is known once complete: at `now`, rules read the dates whose end is ≤ now; only the window
reads today and later (in replay: observed weather used as forecast).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine import downy, incubation, powdery, tankmix, window
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.decision import EvalContext
from farmcore.apps.advisor.engine.gate import local_midnight_utc, season_gate
from farmcore.apps.advisor.engine.protection import Cover, latest_cover
from farmcore.apps.advisor.engine.types import (
    DATA_GAP,
    EXPIRED,
    REPLAY_FORECAST,
    WASHED_OFF,
    Advice,
    Flag,
    HourlyObs,
    RuleResult,
    Spray,
    aware_utc,
)

DAILY_LABEL = "daily regional data; 2-calendar-day approximation"
WINDOW_RULE = "spray_window_daily"
DOWNY_THRESHOLDS = (
    "daily.downy_days",
    "downy_rain_min_mm",
    "downy_temp_min_c",
    "downy_event_max_days",
)
WINDOW_THRESHOLDS = (
    "daily.window_wind_max_ms",
    "daily.window_rain_max_mm",
    "daily.window_rain_next_max_mm",
    "window_sulphur_tmax_c",
    "window_horizon_h",
    "calendar.eval_hour_local",
)
DIURNAL_THRESHOLDS = ("daily.t_min_hour_local", "daily.t_max_hour_local")
INCUB_THRESHOLDS = (
    "daily.incub_daily_rh_split",
    "incub_tmin",
    "incub_alert_pct",
    "incub_symptoms_pct",
)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
SYNTHETIC_NOTE = "derived: hourly T reconstructed from daily Tmin/Tmax (sinusoidal), not measured"


@dataclass(frozen=True, slots=True)
class DailyObs:
    """One local calendar day of regional weather; `None` means missing, never zero."""

    date: date
    precip_mm: float | None = None
    tmean_c: float | None = None
    tmin_c: float | None = None
    tmax_c: float | None = None
    rh_pct: float | None = None
    wind_ms: float | None = None


def _synthetic_t(hour: float, tmin: float, tmax: float, h_min: float, h_max: float) -> float:
    mid, amp = (tmin + tmax) / 2, (tmax - tmin) / 2
    if h_min <= hour <= h_max:
        return mid - amp * math.cos(math.pi * (hour - h_min) / (h_max - h_min))
    since_max = (hour - h_max) % 24
    return mid + amp * math.cos(math.pi * since_max / (24 - (h_max - h_min)))


def synthetic_hourly(days: Sequence[DailyObs], config: AdvisorConfig) -> list[HourlyObs]:
    """Hourly rows with T only, one per UTC hour of each local day (23 or 25 on DST days);
    T is `None` when the day lacks Tmin or Tmax. Labelled derived (`SYNTHETIC_NOTE`)."""
    tz = ZoneInfo(config.local_tz)
    h_min = config.daily.t_min_hour_local.value
    h_max = config.daily.t_max_hour_local.value
    out: list[HourlyObs] = []
    for o in sorted(days, key=lambda o: o.date):
        start = local_midnight_utc(o.date, config.local_tz)
        end = local_midnight_utc(o.date + DAY, config.local_tz)
        ts = start
        while ts < end:
            local = ts.astimezone(tz)
            hour = local.hour + local.minute / 60
            t = (
                None
                if o.tmin_c is None or o.tmax_c is None
                else round(_synthetic_t(hour, o.tmin_c, o.tmax_c, h_min, h_max), 6)
            )
            out.append(HourlyObs(ts=ts, t_c=t))
            ts += HOUR
    return out


class DailySeries:
    """Daily rows by date, prepared once (the season simulation evaluates them every day)."""

    def __init__(self, days: Sequence[DailyObs], config: AdvisorConfig) -> None:
        self.config = config
        self.rows = sorted(days, key=lambda o: o.date)
        self.by_date = {o.date: o for o in self.rows}
        self._end_cache: dict[date, datetime] = {}
        self.ends = [self.day_end(o.date) for o in self.rows]
        # Each day's rain as one row at its end (see `_covers`).
        self.rain_rows = [
            HourlyObs(ts=end, rr1_mm=o.precip_mm)
            for o, end in zip(self.rows, self.ends, strict=True)
        ]
        # Gubler–Thomas reads only the powdery period, so only those days get synthetic hours.
        start, end = config.powdery_start.value, config.powdery_end.value
        self.hourly_t = synthetic_hourly([o for o in self.rows if start <= o.date <= end], config)
        self._hourly_ts = [o.ts for o in self.hourly_t]

    def hourly_t_until(self, end: datetime) -> list[HourlyObs]:
        """Synthetic hourly T rows (powdery period only) with ts < `end`."""
        return self.hourly_t[: bisect_left(self._hourly_ts, end)]

    def day_end(self, d: date) -> datetime:
        """End of local date `d` (local midnight), in UTC; cached."""
        end = self._end_cache.get(d)
        if end is None:
            end = self._end_cache[d] = local_midnight_utc(d + DAY, self.config.local_tz)
        return end

    def n_past(self, now: datetime) -> int:
        """How many leading rows are complete at `now`."""
        return bisect_right(self.ends, now)

    def past(self, now: datetime) -> list[DailyObs]:
        """Days complete at `now`."""
        return self.rows[: self.n_past(now)]


@dataclass(frozen=True, slots=True)
class DailyScan:
    events: list[datetime]
    r48_mm: float | None  # rain over the `daily.downy_days` dates ending at the latest row
    t48_c: float | None
    data_gap_days: list[date]
    event_windows: dict[datetime, tuple[float, float | None]]  # event -> (rain, mean T)


def _tmean(o: DailyObs) -> float | None:
    if o.tmean_c is not None:
        return o.tmean_c
    if o.tmin_c is not None and o.tmax_c is not None:
        return (o.tmin_c + o.tmax_c) / 2
    return None


def scan_3_10(s: DailySeries, past: Sequence[DailyObs], now: datetime) -> DailyScan:
    config = s.config
    n = config.daily.downy_days.value
    gate = config.season_gate.value
    lookback = now - timedelta(days=config.downy_event_max_days.value)
    events: list[datetime] = []
    windows: dict[datetime, tuple[float, float | None]] = {}
    gaps: list[date] = []
    armed = True
    rain: float | None = None  # reported at the latest row present, as the hourly r48
    temp: float | None = None
    # Scan through the last complete local date at `now`, not the last row: days missing at the
    # end of the feed (a stopped feed, ingest lag) count as gaps in the lookback.
    last = now.astimezone(ZoneInfo(config.local_tz)).date() - DAY
    last_row = past[-1].date if past else None
    d = gate
    while d <= last:
        rows = [s.by_date.get(d - DAY * k) for k in range(n)]
        rains = [o.precip_mm for o in rows if o is not None and o.precip_mm is not None]
        temps = [t for t in (_tmean(o) for o in rows if o is not None) if t is not None]
        day_rain = round(sum(rains), 6)
        day_temp = sum(temps) / len(temps) if temps else None
        if last_row is not None and d <= last_row:
            rain, temp = day_rain, day_temp
        if s.day_end(d) > lookback and len(rains) < n:
            gaps.append(d)
        condition = (
            day_rain >= config.downy_rain_min_mm.value
            and day_temp is not None
            and day_temp >= config.downy_temp_min_c.value
        )
        if condition and armed:
            events.append(s.day_end(d))
            windows[s.day_end(d)] = (day_rain, day_temp)
            armed = False
        elif day_rain < config.downy_rain_min_mm.value:
            armed = True
        d += DAY
    return DailyScan(events, rain, temp, gaps, windows)


@dataclass(frozen=True, slots=True)
class DailyWindow:
    open_now: bool
    next_window: tuple[datetime, datetime] | None
    used_future: bool
    reason: RuleResult


def _closed(s: DailySeries, d: date, sulphur: bool) -> list[str]:
    config, daily = s.config, s.config.daily
    obs, nxt = s.by_date.get(d), s.by_date.get(d + DAY)
    if obs is None:
        return ["no_data"]
    closed: list[str] = []
    if obs.wind_ms is None or obs.wind_ms > daily.window_wind_max_ms.value:
        closed.append("wind")
    if obs.precip_mm is None or obs.precip_mm >= daily.window_rain_max_mm.value:
        closed.append("rain")
    if nxt is None or nxt.precip_mm is None or nxt.precip_mm >= daily.window_rain_next_max_mm.value:
        closed.append("rain_next")
    if sulphur and (obs.tmax_c is None or obs.tmax_c >= config.window_sulphur_tmax_c.value):
        closed.append("sulphur_temp")
    return closed


def day_open(s: DailySeries, d: date, *, sulphur: bool = False) -> bool:
    return not _closed(s, d, sulphur)


def daily_window(s: DailySeries, now: datetime, *, sulphur: bool = False) -> DailyWindow:
    config = s.config
    tz = ZoneInfo(config.local_tz)
    today = now.astimezone(tz).date()
    horizon_days = math.ceil(config.window_horizon_h.value / 24)
    hour = config.calendar.eval_hour_local.value
    next_window: tuple[datetime, datetime] | None = None
    for i in range(horizon_days):
        d = today + DAY * i
        if day_open(s, d, sulphur=sulphur):
            opens = datetime.combine(d, time(hour), tzinfo=tz).astimezone(UTC)
            next_window = (max(now, opens), s.day_end(d))
            break
    used_future = any(today + DAY * i in s.by_date for i in range(horizon_days + 1))
    closed_now = _closed(s, today, sulphur)
    obs = s.by_date.get(today)
    inputs = {
        "now": now,
        "day": today,
        "wind_ms": obs.wind_ms if obs else None,
        "precip_mm": obs.precip_mm if obs else None,
        "tmax_c": obs.tmax_c if obs else None,
        "sulphur": sulphur,
        "closed_by": closed_now,
        "next_window": next_window,
    }
    open_now = next_window is not None and not closed_now
    note = f"{DAILY_LABEL}; likely workable day, not compliance"
    if used_future:
        note = f"{note}; {window.REPLAY_NOTE}"
    reason = config.explain(
        WINDOW_RULE, WINDOW_THRESHOLDS, inputs, "open" if open_now else "closed", note
    )
    return DailyWindow(open_now, next_window, used_future, reason)


def _covers(
    s: DailySeries, past: Sequence[DailyObs], sprays: Sequence[Spray], now: datetime
) -> dict[str, Cover]:
    # Each complete day's rain as one row at its end (`past` is a prefix of `s.rows`), so
    # `protection.latest_cover` sums whole days after the spray, the spray day included.
    rows = s.rain_rows[: len(past)]
    tz = ZoneInfo(s.config.local_tz)
    out: dict[str, Cover] = {}
    for disease in ("downy", "powdery"):
        cover = latest_cover(rows, sprays, s.config, now, disease)
        gap = False
        if cover.spray is not None:
            first = cover.spray.ts.astimezone(tz).date()
            span = (
                [first + DAY * i for i in range((past[-1].date - first).days + 1)] if past else []
            )
            gap = any(s.by_date.get(d) is None or s.by_date[d].precip_mm is None for d in span)
            note = f"{cover.reason.note} {DAILY_LABEL}: rain per day from the spray day".strip()
            reason = replace(
                cover.reason, inputs={**cover.reason.inputs, "rain_data_gap": gap}, note=note
            )
            cover = replace(cover, data_gap=gap, reason=reason)
        out[disease] = cover
    return out


def evaluate_daily(
    days: Sequence[DailyObs] | DailySeries,
    sprays: Sequence[Spray],
    config: AdvisorConfig,
    now: datetime,
) -> Advice:
    """Explained advice from daily rows and past sprays at `now` (the daily `evaluate`)."""
    now = aware_utc(now, "evaluate_daily: `now`")
    s = days if isinstance(days, DailySeries) else DailySeries(days, config)
    started, gate = season_gate(config, now)
    if not started:
        return Advice(decision="none", reasons=[gate])

    past = s.past(now)
    done = sorted((sp for sp in sprays if sp.ts <= now), key=lambda sp: sp.ts)
    flags: set[Flag] = set()
    covers = _covers(s, past, done, now)
    downy_cover = covers["downy"]
    for cover in covers.values():
        if cover.washed_off:
            flags.add(WASHED_OFF)
        if cover.expired:
            flags.add(EXPIRED)
        if cover.data_gap:
            flags.add(DATA_GAP)

    scan = scan_3_10(s, past, now)
    if scan.data_gap_days:
        flags.add(DATA_GAP)
    lookback_start = now - timedelta(days=config.downy_event_max_days.value)
    recent = [e for e in scan.events if e > lookback_start]
    downy_sprays = [sp.ts for sp in done if any("downy" in p.targets for p in sp.products)]
    active = [e for e in recent if not any(ts >= e for ts in downy_sprays)]
    verdict = "risk" if active else ("covered" if recent else "no risk")
    note = DAILY_LABEL
    if active:
        note = f"infection event in lookback, not covered; {note}"
    event_at = max(active) if active else (max(recent) if recent else None)
    event_rain, event_temp = scan.event_windows[event_at] if event_at else (None, None)
    downy_reason = config.explain(
        downy.RULE,
        DOWNY_THRESHOLDS,
        {
            "event_at": event_at,
            "event_r48_mm": event_rain,
            "event_t48_c": event_temp,
            "r48_mm": scan.r48_mm,  # 2 calendar days, under the hourly key names
            "t48_c": scan.t48_c,
            "events": scan.events,
            "recent_events": recent,
            "active_events": active,
            "lookback_start": lookback_start,
            "data_gap_days": len(scan.data_gap_days),
        },
        verdict,
        note,
    )
    reasons = [gate, downy_reason, covers["downy"].reason, covers["powdery"].reason]
    advice = Advice(
        decision="none", reasons=reasons, mm_since_spray=downy_cover.mm_since_spray, flags=flags
    )
    copper: DailyWindow | None = None  # set when a downy pass is due
    if recent and downy_cover.valid:
        advice.decision = "protected"
        advice.protected_until = downy_cover.until
    elif active:
        products = [config.default_downy_product]
        win = daily_window(s, now)
        reasons.append(win.reason)
        if win.used_future:
            flags.add(REPLAY_FORECAST)
        advice.decision = "spray" if win.open_now else "wait"
        advice.next_window = win.next_window
        advice.products_recommended = products
        advice.passes, advice.treatments = 1, len(products)
        copper = win

    advice.reasons.append(_incubation(s, past, scan.events, now))
    past_end = s.day_end(past[-1].date) if past else now
    _powdery(s, advice, covers, done, now, past_end, copper)
    return advice


def _split_config(config: AdvisorConfig) -> AdvisorConfig:
    """`config` with the column split read from `daily.incub_daily_rh_split`, so the hourly
    lookup (interpolation, ramp from `incub_tmin`) serves the daily table. A local copy: the
    caller's `incub_humid_rh_hourly` is untouched."""
    return config.model_copy(update={"incub_humid_rh_hourly": config.daily.incub_daily_rh_split})


def goidanich_daily_rate(tmean_c: float, rh_pct: float, config: AdvisorConfig) -> float:
    """Table rate (%/day) at a daily mean T and RH, humid column at RH ≥ the daily split."""
    return incubation.daily_rate(tmean_c, rh_pct, _split_config(config))


def _incubation(
    s: DailySeries, past: Sequence[DailyObs], events: Sequence[datetime], now: datetime
) -> RuleResult:
    config = s.config
    split = _split_config(config)
    alert, full = config.incub_alert_pct.value, config.incub_symptoms_pct.value
    runs = []
    for event in events:
        pct, used, missing = 0.0, 0, 0
        alert_at = symptoms_at = None
        for o in past:
            end = s.day_end(o.date)
            if end <= event:
                continue
            t = _tmean(o)
            if t is None or o.rh_pct is None:
                missing += 1
                continue
            used += 1
            pct += incubation.daily_rate(t, o.rh_pct, split)
            if alert_at is None and pct >= alert - 1e-9:
                alert_at = end
            if pct >= full - 1e-9:
                pct, symptoms_at = full, end
                break
        runs.append(
            {
                "event": event,
                "pct": round(pct, 6),
                "alert_at": alert_at,
                "symptoms_at": symptoms_at,
                "days": used,
                "missing_days": missing,
            }
        )
    latest = runs[-1] if runs else None
    if latest is None:
        verdict = "no event"
    elif latest["symptoms_at"] is not None:
        verdict = "symptoms expected"
    elif latest["alert_at"] is not None:
        verdict = "alert"
    else:
        verdict = "incubating"
    inputs = {
        "event": latest["event"] if latest else None,
        "pct": latest["pct"] if latest else None,
        "alert_at": latest["alert_at"] if latest else None,
        "symptoms_at": latest["symptoms_at"] if latest else None,
        "missing_days": latest["missing_days"] if latest else 0,
        "events": runs,
    }
    note = f"{incubation.NOTE}; {DAILY_LABEL}; {config.incub_table.note}"
    reason = config.explain(incubation.RULE, INCUB_THRESHOLDS, inputs, verdict, note)
    return replace(reason, source=f"{reason.source}; {config.incub_table.source}")


def _gt_reason(state: powdery.GTState, config: AdvisorConfig) -> RuleResult:
    """`powdery.explain` plus the synthetic-curve hours it ran on (`daily.t_*_hour_local`)."""
    base = powdery.explain(state, config)
    names = (*powdery.THRESHOLDS, *DIURNAL_THRESHOLDS)
    note = f"temperature-only risk phase; {SYNTHETIC_NOTE}; {DAILY_LABEL}"
    return config.explain(powdery.RULE, names, base.inputs, base.verdict, note)


def _sulphur_window(s: DailySeries, now: datetime) -> DailyWindow:
    """The daily window with the sulphur Tmax limit; the reason names it when it bites (as
    `powdery.sulphur_window`)."""
    win = daily_window(s, now, sulphur=True)
    if "sulphur_temp" not in win.reason.inputs["closed_by"]:
        return win
    limit = s.config.window_sulphur_tmax_c.value
    note = f"sulphur blocked: T ≥ {limit:g} °C (window_sulphur_tmax_c); {win.reason.note}"
    return replace(win, reason=replace(win.reason, note=note))


def _blocked_cause(s: DailySeries, copper: DailyWindow) -> str:
    """Why sulphur does not start with copper (as `tankmix._blocked_cause`): copper's day fails
    the sulphur Tmax limit, or has no Tmax."""
    if copper.next_window is None:
        return "no spray window within the horizon, sulphur not added"
    day = copper.next_window[0].astimezone(ZoneInfo(s.config.local_tz)).date()
    obs = s.by_date.get(day)
    limit = s.config.window_sulphur_tmax_c.value
    if obs is None or obs.tmax_c is None:
        return f"sulphur blocked: no Tmax on {day} in copper's window (window_sulphur_tmax_c)"
    if obs.tmax_c >= limit:
        return (
            f"sulphur blocked: T ≥ {limit:g} °C (window_sulphur_tmax_c), Tmax {obs.tmax_c:g} °C "
            f"on {day} in copper's window"
        )
    return "sulphur window does not start with copper's"


def _powdery(
    s: DailySeries,
    advice: Advice,
    covers: dict[str, Cover],
    sprays: list[Spray],
    now: datetime,
    past_end: datetime,
    copper: DailyWindow | None,
) -> None:
    """Gubler–Thomas on synthetic hourly T, the period cover, the sulphur-only pass and the
    tank-mix; mirrors `powdery.rule` and `tankmix.rule` (same reasons and verdicts) on the daily
    window."""
    config = s.config
    hourly = s.hourly_t_until(past_end)
    state = powdery.gubler_thomas(hourly, config, now)
    advice.reasons.append(_gt_reason(state, config))
    downy_due = copper is not None
    if not state.in_period:
        verdict = "outside powdery period" if downy_due else "no downy pass due"
        _tankmix_reason(advice, config, downy_due, False, None, verdict)
        return

    ctx = EvalContext(hourly, hourly, sprays, config, now, covers, advice, [], downy_due)
    cover, cover_reason = powdery.period_cover(ctx, state)
    advice.reasons = [cover_reason if r.rule == cover_reason.rule else r for r in advice.reasons]
    if not covers["downy"].expired:
        advice.flags.discard(EXPIRED)
    if cover.expired:
        advice.flags.add(EXPIRED)

    if not cover.ended:
        verdict = "covered"
    elif downy_due:
        verdict = "sulphur due; no sulphur-only pass, tankmix decides (downy pass due)"
    else:
        verdict = "sulphur due"
    inputs = {
        "in_period": True,
        "cover_until": cover.until,
        "never_applied": cover.spray_ts is None,
        "downy_due": downy_due,
        "downy_protected_until": advice.protected_until,
    }
    advice.reasons.append(
        config.explain(powdery.PASS_RULE, ("powdery_start", "powdery_end"), inputs, verdict)
    )

    if not downy_due:
        if cover.ended:
            win = _sulphur_window(s, now)
            advice.reasons.append(win.reason)
            if win.used_future:
                advice.flags.add(REPLAY_FORECAST)
            advice.decision = "spray" if win.open_now else "wait"
            advice.next_window = win.next_window
            advice.products_recommended = [config.default_powdery_product]
            advice.passes, advice.treatments = 1, 1
        _tankmix_reason(advice, config, False, True, cover.until, "no downy pass due")
        return

    assert copper is not None  # downy_due
    ending = timedelta(days=config.tankmix_cover_ending_days.value)
    if not (cover.ended or cover.until - now <= ending):
        _tankmix_reason(advice, config, True, True, cover.until, "copper only: powdery cover holds")
        return
    _tankmix_reason(advice, config, True, True, cover.until, "tank-mix due: sulphur with copper")
    sulphur = daily_window(s, now, sulphur=True)
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
        else f"copper alone: {_blocked_cause(s, copper)}"
    )
    advice.reasons.append(replace(sulphur.reason, note=f"{outcome}; {sulphur.reason.note}"))
    if not joins:
        return
    advice.next_window = sulphur.next_window
    advice.products_recommended = [*advice.products_recommended, config.default_powdery_product]
    advice.passes, advice.treatments = 1, len(advice.products_recommended)


def _tankmix_reason(
    advice: Advice,
    config: AdvisorConfig,
    downy_due: bool,
    in_period: bool,
    until: datetime | None,
    verdict: str,
) -> None:
    inputs = {"downy_due": downy_due, "in_period": in_period, "powdery_cover_until": until}
    advice.reasons.append(config.explain(tankmix.RULE, tankmix.THRESHOLDS, inputs, verdict))
