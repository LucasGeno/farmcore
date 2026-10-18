"""Scoped demo reset. HTTP authorization lives in routes_demo; scene logic belongs to apps."""

from dataclasses import asdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text

from farmcore.core import jobs, logs
from farmcore.core.access import FarmNotSeeded, default_farm
from farmcore.core.clock import clock_row, get_clock, set_replay
from farmcore.core.demo import Preset
from farmcore.core.manifest import import_string
from farmcore.core.models.base import Base
from farmcore.core.models.canonical import (
    AccessLog,
    Advice,
    Asset,
    Event,
    ExtId,
    Farm,
    Log,
    Observation,
    Parcel,
)
from farmcore.core.models.kernel import ClockState
from farmcore.core.timeline import post_event
from farmcore.db import session_scope


def demo_farm(session, settings, farm_id=None):
    try:
        farm = default_farm(session, settings)
    except FarmNotSeeded as exc:
        raise HTTPException(
            status_code=503, detail="Seed the demo farm before using controls."
        ) from exc
    if farm_id is not None and farm_id != farm.id:
        raise HTTPException(
            status_code=403, detail="Only the registered default demo farm may reset."
        )
    return farm


def metadata(row):
    return dict((row.job_runs or {}).get("demo") or {}) if row else {}


def snapshot(session, settings):
    """Read-only: never use Repo (read auditing) or clock_row (lazy initialization)."""
    farm = demo_farm(session, settings)
    row = session.get(ClockState, 1)
    now = row.replay_now if row and row.mode == "replay" else datetime.now(UTC)
    if not row:
        now = settings.replay_start
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(settings.display_tz))
    total = session.scalar(
        select(func.count()).select_from(Parcel).where(Parcel.farm_id == farm.id)
    )
    ranked = (
        select(
            Advice.decision,
            func.row_number()
            .over(
                partition_by=Advice.parcel_id,
                order_by=(Advice.evaluated_at.desc(), Advice.id.desc()),
            )
            .label("rank"),
        )
        .where(Advice.farm_id == farm.id, Advice.evaluated_at <= now)
        .subquery()
    )
    latest = session.scalars(select(ranked.c.decision).where(ranked.c.rank == 1))
    counts = dict.fromkeys(("spray", "wait", "protected", "none"), 0)
    for decision in latest:
        counts[decision] += 1
    counts["not evaluated"] = total - sum(counts.values())
    chat = (farm.props or {}).get("chat") or {}
    meta = metadata(row)
    job_state = row.job_runs if row else {}
    push_runs = [
        job_state.get("last_run", {}).get("chat.push_advice"),
        job_state.get("farm_last_run", {}).get(str(farm.id), {}).get("chat.push_advice"),
    ]
    last_push = max((value for value in push_runs if value), default=None)
    return {
        "farm": {"id": farm.id, "name": farm.name},
        "now": now,
        "mode": row.mode if row else settings.clock_mode,
        "counts": counts,
        "bot_linked": bool(chat.get("telegram_chat_ids")),
        "push_cursor": chat.get("push_cursor"),
        "last_push_attempt": datetime.fromisoformat(last_push) if last_push else None,
        "generation": meta.get("generation", 0),
        "selected_preset": meta.get("preset", "demo-start"),
        "live_reasons": live_reasons(session, farm),
        "notice": meta.get("notice"),
        "input_time": now.astimezone(ZoneInfo(settings.display_tz)).strftime("%Y-%m-%dT%H:%M"),
        "runs": ((row.job_runs or {}).get("last_step") or {}).get("runs", []) if row else [],
    }


def _clear(session, farm, manifests=()):
    """Delete only the demo farm's transient records; keep identities and chat bindings. Apps
    undo their own preset-only changes through `Manifest.demo_reset` hooks."""
    for manifest in manifests:
        for path in manifest.demo_reset:
            import_string(path)(session, farm)
    log_ids = list(session.scalars(select(Log.id).where(Log.farm_id == farm.id)))
    session.execute(delete(ExtId).where(ExtId.model == "log", ExtId.res_id.in_(log_ids)))
    session.execute(delete(Log).where(Log.farm_id == farm.id))  # child rows cascade
    session.execute(delete(Advice).where(Advice.farm_id == farm.id))
    session.execute(
        delete(Event).where(
            Event.farm_id == farm.id,
            Event.kind.startswith("log.") | Event.kind.startswith("advice."),
        )
    )
    # Initial farm creation is the existing seed watermark; never substitute replay time.
    session.execute(
        delete(AccessLog).where(AccessLog.farm_id == farm.id, AccessLog.ts >= farm.created_at)
    )


def _local_time(value):
    return value.astimezone(ZoneInfo("Europe/Paris")).strftime("%d %b %Y %H:%M %Z")


def live_reasons(session, farm):
    """Honest refusal: no scheduler start or automatic current-data fetch is added here."""
    last = session.scalar(
        select(func.max(Observation.ts))
        .join(Asset, Asset.id == Observation.asset_id)
        .where(Asset.farm_id == farm.id, Asset.kind == "station")
    )
    reasons = [f"No station data after {_local_time(last)}." if last else "No station data loaded."]
    table = Base.metadata.tables.get("forecast_value")
    forecast_last = (
        session.scalar(select(func.max(table.c.ts)).where(table.c.farm_id == farm.id))
        if table is not None
        else None
    )
    reasons.append(
        f"No forecast data after {_local_time(forecast_last)}."
        if forecast_last
        else "No forecast loaded."
    )
    reasons.append(
        "Live ingestion is not configured: station jobs read a saved CSV, forecast fetching "
        "is CLI-only, and web/bot schedulers need a coordinated restart. Live remains unavailable."
    )
    return reasons


def apply_action(
    factory,
    manifests,
    settings,
    *,
    preset: Preset,
    farm_id: int,
    generation: int,
    action: str,
    target: datetime | None = None,
):
    # Same coordinator lock as web/CLI replay; hold it until due jobs have finished.
    with session_scope(factory) as coordinator:
        coordinator.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": jobs.STEP_LOCK_KEY})
        try:
            with session_scope(factory) as session:
                farm = demo_farm(session, settings, farm_id)
                logs.lock_farm(session, farm.id)  # same lock as voice/import operation writes
                farm = session.get(Farm, farm.id, with_for_update=True, populate_existing=True)
                row = clock_row(session, settings)
                meta = metadata(row)
                if generation != meta.get("generation", 0):
                    raise HTTPException(
                        status_code=409, detail="Demo state changed; preview again."
                    )
                new_meta = {**meta, "generation": generation + 1, "notice": None}
                if action == "live":
                    notice = " ".join(live_reasons(session, farm))
                    row.job_runs = {**row.job_runs, "demo": {**new_meta, "notice": notice}}
                    post_event(
                        session,
                        farm_id=farm.id,
                        res_model="farm",
                        res_id=farm.id,
                        kind="demo.live_refused",
                        body=f"Demo data: live request refused. {notice}",
                        payload={"demo_data": True},
                        actor="admin",
                        source_app="core",
                        occurred_at=get_clock(session, settings).now(),
                    )
                    return []
                staging = action in {"reset", "preset", "replay"}
                if staging:
                    _clear(session, farm, manifests)
                    if preset.scene:
                        import_string(preset.scene)(session, farm, preset.clock_target)
                    new_meta["preset"] = preset.key
                    target = preset.clock_target
                set_replay(session, target, settings)
                state = dict(row.job_runs)
                if staging:
                    schedules = dict(state.get("farm_last_run") or {})
                    schedules.pop(str(farm_id), None)
                    state["farm_last_run"] = schedules
                    state.pop("last_step", None)
                row.job_runs = {**state, "demo": new_meta}
                post_event(
                    session,
                    farm_id=farm.id,
                    res_model="farm",
                    res_id=farm.id,
                    kind=f"demo.{action}",
                    body=(
                        f"Demo data: {action} to {preset.label}."
                        if staging
                        else f"Demo data: clock stepped to {target.isoformat()}."
                    ),
                    payload={"preset": new_meta.get("preset"), "demo_data": True},
                    actor="admin",
                    source_app="core",
                    occurred_at=target,
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Action rolled back ({type(exc).__name__}); no reset changes committed.",
            ) from exc
        # Jobs retain their existing separate transactions; failures are recorded, not hidden.
        runs = jobs.step_locked(
            factory, manifests, target, settings, farm_ids=(farm_id,), invalidate_preview=False
        )
        return [asdict(run) for run in runs]
