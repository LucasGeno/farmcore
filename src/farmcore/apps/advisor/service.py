"""Advisor service: engine inputs from the canonical model, advice rows, the card's view.

Every read goes through a `ScopedRepository` (the job passes `repo_for_job`, routes `get_repo`)
and is bounded by the clock `now`:

- weather: `stations.service.load_hourly(repo, station, 1 Mar local of now's year,
  now + forecast_span(config))` (73 h with the defaults: the window's last horizon hour reads the
  rain of the hours after it), loaded once per farm; the hours after now feed only the engine's
  window search;
- sprays: done spray logs on the parcel with `ts_start <= now`; each quantity's product gives the
  engine its type and targets;
- previous advice: the latest advice for the parcel evaluated before now. A re-evaluation at the
  same now (`run_now` after a confirm) compares with this visit's earlier row at now, i.e. one
  newer (higher id) than every row at another time; a row at now left from an earlier visit (any
  row elsewhere was written after it, e.g. the clock went past it and back, or back then forward
  again) does not count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on

from farmcore.apps.advisor import priority
from farmcore.apps.advisor.engine import Advice as EngineAdvice
from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs, ProductRef, Spray
from farmcore.apps.advisor.engine.config import DEFAULT_REGION
from farmcore.apps.advisor.engine.types import floor_hour
from farmcore.apps.advisor.engine.window import forecast_span
from farmcore.apps.stations import service as stations
from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import (
    Advice,
    Farm,
    Log,
    LogParcel,
    LogSource,
    Product,
    Quantity,
)

SCOPE = "advice:read"
LOGS_SCOPE = "logs:read"
NO_ADVICE = "none"
SPRAY_WITHOUT_PRODUCT = "spray_without_product"
CARD_PATH = "/advisor/parcels/{id}"
SEASON_START = (3, 1)  # (month, day): weather history from 1 Mar of the year


# --- engine inputs -------------------------------------------------------------------------


def unavailable_reason(repo: ScopedRepository) -> str | None:
    try:
        repo.check("stations:read", "observation")
        repo.check("parameters:read", "parameter_version")
    except ScopeDenied:
        return (
            "Assessment limited: current weather/configuration inputs "
            "are not authorized by this grant"
        )
    farm = repo.get(Farm, repo.farm_id, scope=SCOPE)
    reason = (farm.props or {}).get("advisor_unavailable_reason") if farm else None
    if reason:
        return reason
    if stations.station_for_farm(repo) is None:
        return "Assessment unavailable: no weather station attached to this farm"
    return None


def config_for(farm: Farm, now: datetime, tz: str) -> AdvisorConfig:
    """The farm's region (`props.region`, default FRI12) and now's local year."""
    region = str((farm.props or {}).get("region") or DEFAULT_REGION)
    return AdvisorConfig.for_region_year(region, now.astimezone(ZoneInfo(tz)).year)


def series_start(now: datetime, tz: str) -> datetime:
    zone = ZoneInfo(tz)
    month, day = SEASON_START
    return datetime(now.astimezone(zone).year, month, day, tzinfo=zone)


def load_series(
    repo: ScopedRepository, station_id: int, now: datetime, tz: str, config: AdvisorConfig
) -> list[HourlyObs]:
    until = now + forecast_span(config)
    rows = stations.load_hourly(repo, station_id, series_start(now, tz), until)
    return [HourlyObs(**row) for row in rows]


def load_sprays(repo: ScopedRepository, parcel_id: int, now: datetime) -> tuple[list[Spray], int]:
    """Done spray logs on the parcel with `ts_start <= now`, with their products, and the number
    of such logs ignored because no quantity names a known product (they give no cover)."""
    logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == parcel_id)),
        scope=LOGS_SCOPE,
        order_by=(Log.ts_start, Log.id),
    )
    if not logs:
        return [], 0
    stmt = (
        select(Quantity.log_id, Product)
        .join(Product, Product.id == Quantity.product_id)
        .where(Quantity.log_id.in_([log.id for log in logs]))
        .order_by(Quantity.id)
    )
    products: dict[int, list[ProductRef]] = {}
    for log_id, product in repo.execute(stmt, scope=LOGS_SCOPE, resource="quantity"):
        products.setdefault(log_id, []).append(
            ProductRef(
                name=product.name, product_type=product.product_type, targets=product.targets
            )
        )
    sprays = [
        Spray(ts=log.ts_start, products=products[log.id]) for log in logs if log.id in products
    ]
    return sprays, len(logs) - len(sprays)


# --- previous advice -----------------------------------------------------------------------


UNCERTAIN = ("limited", "unavailable")


def state_label(decision: str, props: dict | None) -> str:
    """What a change notice says about one advice row: its decision, except that a limited or
    unavailable assessment with no action is never reported as "none" or "protected"."""
    status = (props or {}).get("assessment_status")
    if status in UNCERTAIN and decision in (NO_ADVICE, "protected"):
        return f"assessment {status}"
    return decision


def previous_decisions(repo: ScopedRepository, now: datetime) -> dict[int, str]:
    """parcel id -> state label (`state_label`) of its latest advice before this evaluation at
    `now` (module doc)."""

    def latest(*where: Any) -> dict[int, tuple[int, str]]:
        """parcel id -> (id, state label) of its latest advice matching `where`."""
        rows = repo.execute(
            select(
                Advice.parcel_id,
                Advice.id,
                Advice.decision,
                Advice.props["assessment_status"].astext,
            )
            .where(Advice.farm_id == repo.farm_id, *where)
            .ext(distinct_on(Advice.parcel_id))
            .order_by(Advice.parcel_id, Advice.evaluated_at.desc(), Advice.id.desc()),
            scope=SCOPE,
            resource="advice",
        )
        return {
            parcel_id: (advice_id, state_label(decision, {"assessment_status": status}))
            for parcel_id, advice_id, decision, status in rows
        }

    others = dict(
        repo.execute(
            select(Advice.parcel_id, func.max(Advice.id))
            .where(Advice.farm_id == repo.farm_id, Advice.evaluated_at != now)
            .group_by(Advice.parcel_id),
            scope=SCOPE,
            resource="advice",
        ).all()
    )
    out = {pid: decision for pid, (_, decision) in latest(Advice.evaluated_at < now).items()}
    for pid, (advice_id, decision) in latest(Advice.evaluated_at == now).items():
        if advice_id > others.get(pid, 0):  # newer than every other row: written on this visit
            out[pid] = decision
    return out


def latest_advice(
    repo: ScopedRepository, now: datetime, *, tz: str, parcel_id: int | None = None
) -> dict[int, Advice]:
    """parcel id -> its latest advice row with `evaluated_at <= now` (one parcel if given)."""
    where = [Advice.farm_id == repo.farm_id, Advice.evaluated_at <= now]
    if parcel_id is not None:
        where.append(Advice.parcel_id == parcel_id)
    # Current views need assessment facts, not the private replay evidence. Build
    # detached projections so the identity map always retains the full saved row.
    columns = [
        (
            Advice.props.op("-")("input_snapshot").op("-")("effective_config").label("props")
            if column.name == "props"
            else column
        )
        for column in Advice.__table__.columns
    ]
    rows = repo.execute(
        select(*columns)
        .where(*where)
        .ext(distinct_on(Advice.parcel_id))
        .order_by(Advice.parcel_id, Advice.evaluated_at.desc(), Advice.id.desc()),
        scope=SCOPE,
        resource="advice",
    ).mappings()
    result = {row["parcel_id"]: Advice(**row) for row in rows}
    status, reason = current_availability(repo, now, tz)
    if reason:
        # Transient read projection: saved historical evidence is never modified.
        projected = {}
        for pid, a in result.items():
            props = a.props or {}
            # An observed positive risk remains informative under partial coverage.
            positive = status == "limited" and a.decision in ("spray", "wait")
            view = {c.name: getattr(a, c.name) for c in Advice.__table__.columns}
            view.update(
                props={
                    **props,
                    "assessment_status": status,
                    "assessment_status_at_evaluation": props.get(
                        "assessment_status", "legacy-unknown"
                    ),
                    "current_availability": {"status": status, "reason": reason},
                    "headline": (
                        (props.get("headline", "") + "; " + reason) if positive else reason
                    ),
                }
            )
            if not positive:
                view.update(
                    decision="none",
                    reasons=[],
                    products=[],
                    protected_until=None,
                    next_window_start=None,
                    next_window_end=None,
                )
                view["props"].update(flags=["data_gap"], passes=0, treatments=0)
            projected[pid] = Advice(**view)
        return projected
    return result


# --- advice rows ---------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """Engine values -> JSON: aware datetimes ISO 8601 in UTC, dates ISO, tuples/sets lists,
    dataclasses dicts."""
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo else value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def _product(p: ProductRef) -> dict[str, Any]:
    return {"name": p.name, "product_type": p.product_type, "targets": list(p.targets)}


def _fmt_mm(mm: float | None) -> str:
    return f"{mm:.1f} mm" if mm is not None else "? mm"


def _window_cutoff(now: datetime, config: AdvisorConfig) -> datetime:
    """The last hour the engine's window can reach: its horizon. `load_series` loads
    `now + forecast_span(config)`, so the rain lookahead of every horizon hour is in the series
    and the horizon is never cut short (tests/apps/advisor/test_forecast_span.py)."""
    return floor_hour(now) + timedelta(hours=config.window_horizon_h.value)


def _window_text(
    window: tuple[datetime, datetime] | None, now: datetime, tz: str, config: AdvisorConfig
) -> str:
    """`today 06–10 h`, `today 06 h – Sat 13 Apr 05 h` when the window ends another day, or
    `from today 06 h` when it runs to the end of the forecast (its end is only the cutoff)."""
    if window is None:
        return "no spray window in the next 72 h"
    zone = ZoneInfo(tz)
    start, end = (t.astimezone(zone) for t in window)
    open_ended = window[1] >= _window_cutoff(now, config)
    today = now.astimezone(zone).date()
    if start.date() == today:
        day = "today"
    elif start.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{start:%a %d %b}"
    if open_ended:
        return f"from {day} {start:%H} h"
    if end.date() == start.date():
        return f"{day} {start:%H}–{end:%H} h"
    return f"{day} {start:%H} h – {end:%a %d %b %H} h"


def event_rain_mm(
    series: Sequence[HourlyObs], event: datetime, config: AdvisorConfig
) -> float | None:
    """Rain in the 3-10 window that ended at `event` (rows with ts in (event − window, event])."""
    since = event - timedelta(hours=config.downy_window_h.value)
    values = [o.rr1_mm for o in series if since < o.ts <= event and o.rr1_mm is not None]
    return round(sum(values), 1) if values else None


def headline(
    advice: EngineAdvice,
    series: Sequence[HourlyObs],
    config: AdvisorConfig,
    now: datetime,
    tz: str,
) -> str:
    """One line for the card, the timeline and the bot, e.g. "Spray copper today 06–10 h:
    infection risk after 10.3 mm rain, not protected"."""
    opinion = advice.reason("parameter_review")
    if opinion is not None:
        return (
            f"Assessment limited: parameter version {opinion.verdict}; "
            "human opinion is not field accuracy evidence"
        )
    if advice.assessment_status == "unavailable":
        return "Assessment unavailable: missing hourly observations"
    if advice.assessment_status == "limited" and advice.decision in ("none", "protected"):
        return "Assessment limited: incomplete required inputs; infection risk is unknown"
    downy = advice.reason("downy_3_10")
    active = list(downy.inputs.get("active_events") or []) if downy else []
    names = " + ".join(p.name for p in advice.products_recommended)
    if active:
        # One source with the card's 3-10 row: the reason's event rain. `headline` only sees a
        # fresh engine Advice, which always carries it; the series sum guards a reason without it.
        if "event_r48_mm" in downy.inputs:
            rain = downy.inputs["event_r48_mm"]
        else:
            rain = event_rain_mm(series, max(active), config)
        why = f"infection risk after {_fmt_mm(rain)} rain, not protected"
    elif advice.products_recommended:
        why = "powdery cover ended"
    else:
        why = "no pass due"
    window = _window_text(advice.next_window, now, tz, config)
    if advice.decision == "spray":
        return f"Spray {names} {window}: {why}"
    if advice.decision == "wait":
        return f"Wait: {why}; next window {window}"
    if advice.decision == "protected":
        until = advice.protected_until
        until_s = f"{until.astimezone(ZoneInfo(tz)):%d %b %H:%M}" if until else "?"
        return f"Protected until {until_s}: {_fmt_mm(advice.mm_since_spray)} rain since spray"
    gate = advice.reason("season_gate")
    if gate is not None and gate.verdict == "season not started":
        return "No action: season not started"
    return "No action: no infection risk"


def incubation_pct(advice: EngineAdvice) -> float | None:
    reason = advice.reason("downy_incubation")
    return (
        reason.inputs.get("pct") if reason and reason.inputs.get("coverage") != "limited" else None
    )


def advice_row(
    advice: EngineAdvice,
    *,
    series: Sequence[HourlyObs],
    config: AdvisorConfig,
    farm_id: int,
    parcel_id: int,
    now: datetime,
    tz: str,
    ignored_sprays: int = 0,
    effective=None,
    input_snapshot: dict | None = None,
) -> Advice:
    """`ignored_sprays` > 0 (done spray logs without a known product) adds `spray_without_product`
    to the flags."""
    window = advice.next_window
    flags = set(advice.flags) | ({SPRAY_WITHOUT_PRODUCT} if ignored_sprays else set())
    return Advice(
        farm_id=farm_id,
        parcel_id=parcel_id,
        evaluated_at=now,
        decision=advice.decision,
        reasons=[jsonable(r) for r in advice.reasons],
        next_window_start=window[0] if window else None,
        next_window_end=window[1] if window else None,
        protected_until=advice.protected_until,
        products=[_product(p) for p in advice.products_recommended],
        props={
            "headline": headline(advice, series, config, now, tz),
            "assessment_status": advice.assessment_status,
            # The version row is immutable and holds the configuration; copy it only
            # when there is no version to point at.
            **(
                {}
                if effective and effective.version
                else {"effective_config": config.model_dump(mode="json")}
            ),
            "config_version": effective.identity()
            if effective
            else {"id": None, "status": "unreviewed", "accuracy_claim": False},
            "input_snapshot": input_snapshot or {},
            "flags": sorted(flags),
            "mm_since_spray": advice.mm_since_spray,
            "incubation_pct": incubation_pct(advice),
            "passes": advice.passes,
            "treatments": advice.treatments,
        },
    )


def advice_json(row: Advice) -> dict[str, Any]:
    """The stored advice row as JSON (the card's `.json`, the bot)."""
    props = row.props or {}
    return {
        "assessment_status": props.get("assessment_status", "legacy-unknown"),
        "assessment_status_at_evaluation": props.get(
            "assessment_status_at_evaluation", props.get("assessment_status", "legacy-unknown")
        ),
        "current_availability": props.get("current_availability"),
        "config_version": props.get(
            "config_version", {"id": None, "status": "legacy-unversioned", "accuracy_claim": False}
        ),
        "id": row.id,
        "parcel_id": row.parcel_id,
        "evaluated_at": jsonable(row.evaluated_at),
        "decision": row.decision,
        "headline": props.get("headline", ""),
        "reasons": row.reasons,
        "next_window": (
            [jsonable(row.next_window_start), jsonable(row.next_window_end)]
            if row.next_window_start
            else None
        ),
        "protected_until": jsonable(row.protected_until),
        "mm_since_spray": props.get("mm_since_spray"),
        "incubation_pct": props.get("incubation_pct"),
        "flags": props.get("flags", []),
        "products": row.products,
        "passes": props.get("passes", 0),
        "treatments": props.get("treatments", 0),
        "card_path": CARD_PATH.format(id=row.parcel_id),
        **priority.as_dict(priority.of_advice(row)),
    }


def empty_advice_json(parcel_id: int, headline_text: str) -> dict[str, Any]:
    """`advice_json`'s shape for a parcel with no advice up to now: decision `none`, id None."""
    return {
        "assessment_status": "unavailable",
        "config_version": {"id": None, "status": "legacy-unversioned", "accuracy_claim": False},
        "id": None,
        "parcel_id": parcel_id,
        "evaluated_at": None,
        "decision": NO_ADVICE,
        "headline": headline_text,
        "reasons": [],
        "next_window": None,
        "protected_until": None,
        "mm_since_spray": None,
        "incubation_pct": None,
        "flags": [],
        "products": [],
        "passes": 0,
        "treatments": 0,
        "card_path": CARD_PATH.format(id=parcel_id),
        **priority.as_dict(None),
    }


def forecast_edge(evaluated_at: datetime, horizon_h: Any) -> datetime | None:
    """Where the advice's forecast ended: the engine scans spray windows from the evaluation
    hour (`floor_hour`) for `window_horizon_h` hours (engine/window.py). None without a horizon."""
    if horizon_h is None:
        return None
    return floor_hour(evaluated_at) + timedelta(hours=int(horizon_h))


def runs_to_forecast_edge(end: datetime | None, evaluated_at: datetime, horizon_h: Any) -> bool:
    """A window ending at the forecast edge has no known end: not a spray deadline. The one rule
    for the card (`routes._window_phrase`) and the summary (`presenter`)."""
    edge = forecast_edge(evaluated_at, horizon_h)
    return end is not None and edge is not None and end >= edge


def input_snapshot(repo, station, parcel_id, series, config, now, tz, ignored):
    """The exact hourly series the engine read, plus the spray evidence, with unknown receipt
    semantics stated explicitly. Station rows are not copied: `hourly_inputs` is what was
    evaluated, and rows written before this shape also carry `observations`."""
    logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == parcel_id)),
        scope=LOGS_SCOPE,
        order_by=(Log.ts_start, Log.id),
    )
    log_ids = [row.id for row in logs]
    quantities = (
        repo.execute(
            select(Quantity, Product)
            .outerjoin(Product, Product.id == Quantity.product_id)
            .where(Quantity.log_id.in_(log_ids))
            .order_by(Quantity.id),
            scope=LOGS_SCOPE,
            resource="quantity",
        ).all()
        if log_ids
        else []
    )

    sources = (
        repo.list(LogSource, LogSource.log_id.in_(log_ids), scope=LOGS_SCOPE, order_by=LogSource.id)
        if log_ids
        else []
    )

    def evidence(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, float) and not isfinite(value):
            return {"rejected": "non-finite", "raw": str(value)}
        if is_dataclass(value) and not isinstance(value, type):
            return evidence(asdict(value))
        if isinstance(value, dict):
            return {str(k): evidence(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [evidence(v) for v in value]
        return jsonable(value)

    def columns(row):
        return {
            col.name: evidence(getattr(row, col.name))
            for col in row.__table__.columns
            if col.name != "geom"
        }

    return {
        "evaluation_at": now.isoformat(),
        "display_tz": tz,
        "config_tz": config.local_tz,
        "algorithm": "FarmCore typed hourly advisor / reviewed-parameters-v1",
        "station": columns(station),
        "observation_receipt": "unknown",
        "observation_issue_time": "unknown",
        "future_observations": "Replay surrogate, not as-issued forecast",
        "loaded_from": series_start(now, tz).isoformat(),
        "loaded_until": (now + forecast_span(config)).isoformat(),
        "hourly_inputs": evidence(series),
        "spray_logs": [columns(log) for log in logs],
        "log_sources": [columns(source) for source in sources],
        "hourly_units": {"rr1_mm": "mm", "t_c": "°C", "rh_pct": "%", "ff_kmh": "km/h"},
        "quantities": [
            {"quantity": columns(q), "product": columns(p) if p else None} for q, p in quantities
        ],
        "ignored_sprays": ignored,
    }


def current_availability(repo, now, tz):
    reason = unavailable_reason(repo)
    if reason:
        return ("limited" if "not authorized" in reason else "unavailable"), reason
    from farmcore.apps.advisor.engine import evaluate
    from farmcore.apps.review.service import resolve

    config = resolve(repo, now, tz).config
    station = stations.station_for_farm(repo)
    series = load_series(repo, station.id, now, tz, config)
    assessment = evaluate(series, [], config, now)
    if assessment.assessment_status == "evaluable":
        return "evaluable", None
    return assessment.assessment_status, headline(assessment, series, config, now, tz)


def saved_config(repo: ScopedRepository, row: Advice) -> dict | None:
    """The configuration a saved assessment used: its own copy (rows saved before configs were
    stored by version) or the immutable parameter version it names."""
    props = row.props or {}
    if "effective_config" in props:
        return props["effective_config"]
    version_id = (props.get("config_version") or {}).get("id")
    if version_id is None:
        return None
    from farmcore.apps.review.models import ParameterVersion

    version = repo.get(ParameterVersion, version_id, scope="parameters:read")
    return version.effective_config if version else None
