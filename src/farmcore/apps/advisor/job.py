"""`advisor.evaluate`: evaluate every parcel of every farm at the clock `now`.

Per farm: the weather series once (`service.load_series`), then per parcel its done sprays, one
`engine.evaluate`, one `advice` row (evaluated_at = now) and an `advice.evaluated` event on the
parcel. When the decision differs from the parcel's previous advice (`service.previous_decisions`;
missing = `none`) it also posts `advice.changed` (payload: parcel alias and name, old, new,
headline, card path). So a first evaluation that yields `none` posts no change. A done spray log
with no known product gives no cover and flags the row `spray_without_product`. Chat (16) pushes
the changes; advisor never imports chat. A farm without a weather station is skipped.

In live mode the job only plans (one durable task per due farm, `core/tasks.py`) and the worker
runs `evaluate_live`, which writes nothing unless its attempt is still current.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.advisor import service
from farmcore.apps.advisor.engine import evaluate as engine_evaluate
from farmcore.apps.advisor.engine.types import RuleResult
from farmcore.apps.advisor.manifest import EVALUATION_INTERVAL, JOB_NAME
from farmcore.apps.fields.seed import alias_number
from farmcore.apps.stations import service as stations
from farmcore.core import tasks
from farmcore.core.access import ScopedRepository, repo_for_job
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.farm_location import time_zone_of
from farmcore.core.logs import lock_farm
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.timeline import post_event
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)
ACTOR = f"job:{JOB_NAME}"
SOURCE_APP = "advisor"
PARCEL_SCOPE = "fields:read"


def evaluate(ctx: JobContext) -> None:
    """Replay: evaluate every farm (or `ctx.farm_ids`) at `ctx.now`, in this transaction. Live:
    only queue each due farm; `farmcore worker` runs `evaluate_live` for it."""
    farm_ids = ctx.session.scalars(
        select(Farm.id).where(Farm.id.in_(ctx.farm_ids))
        if ctx.farm_ids is not None
        else select(Farm.id).order_by(Farm.id)
    ).all()
    if isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        tasks.enqueue_due(ctx.session, JOB_NAME, EVALUATION_INTERVAL, farm_ids)
        return
    for farm_id in farm_ids:
        _lock(ctx.session, farm_id)
        _evaluate(ctx.session, farm_id, ctx.now, ctx.settings)


def evaluate_live(ctx: tasks.TaskContext) -> None:
    """The durable task: one farm at the live now, written only if the attempt is current."""
    with session_scope(ctx.session_factory) as session:
        # Locks before the fence: it takes the farm row FOR KEY SHARE, and upgrading that to
        # FOR UPDATE afterwards could deadlock with another task's publish on the same farm.
        _lock(session, ctx.farm_id)
        if not tasks.publish_if_current(session, ctx.attempt):
            return
        _evaluate(session, ctx.farm_id, get_clock(session, ctx.settings).now(), ctx.settings)


def _lock(session: Session, farm_id: int) -> None:
    lock_farm(session, farm_id)
    session.execute(select(Farm.id).where(Farm.id == farm_id).with_for_update())


def _evaluate(session: Session, farm_id: int, now: datetime, settings: Settings) -> None:
    evaluate_farm(
        repo_for_job(session, farm_id, JOB_NAME), now, time_zone_of(session, farm_id, settings)
    )


def evaluate_farm(repo: ScopedRepository, now: datetime, tz: str) -> int:
    """Evaluate parcels. Caller holds advisory→farm row locks before creating this repo."""
    if service.unavailable_reason(repo):
        return 0
    station = stations.station_for_farm(repo)
    if station is None:
        logger.info("%s: farm %s has no weather station, skipped", JOB_NAME, repo.farm_id)
        return 0
    from farmcore.apps.review.service import resolve

    effective = resolve(repo, now, tz, seed=True)
    config = effective.config
    series = service.load_series(repo, station.id, now, tz, config)
    parcels = sorted(
        repo.list(Parcel, scope=PARCEL_SCOPE), key=lambda p: (alias_number(p.alias), p.alias)
    )
    previous = service.previous_decisions(repo, now)
    session = repo.session
    for parcel in parcels:
        sprays, ignored = service.load_sprays(repo, parcel.id, now)
        advice = engine_evaluate(series, sprays, config, now)
        if effective.status in ("rejected", "unsure"):
            advice.assessment_status = "limited"
            advice.decision = "none"
            advice.protected_until = None
            advice.next_window = None
            advice.products_recommended = []
            advice.passes = advice.treatments = 0
            advice.reasons.append(
                RuleResult(
                    "parameter_review",
                    {"version_id": effective.version.id, "review_id": effective.review.id},
                    {},
                    effective.status,
                    "Recorded human opinion; not field accuracy evidence",
                )
            )
        row = service.advice_row(
            advice,
            series=series,
            config=config,
            farm_id=repo.farm_id,
            parcel_id=parcel.id,
            now=now,
            tz=tz,
            ignored_sprays=ignored,
            effective=effective,
            input_snapshot=service.input_snapshot(
                repo, station, parcel.id, series, config, now, tz, ignored
            ),
        )
        session.add(row)
        session.flush()
        head = row.props["headline"]
        card = service.CARD_PATH.format(id=parcel.id)
        base = {
            "advice_id": row.id,
            "parcel_id": parcel.id,
            "alias": parcel.alias,
            "name": parcel.name,
            "headline": head,
            "card_path": card,
        }
        _post(repo, parcel.id, "advice.evaluated", head, {**base, "decision": row.decision}, now)
        old = previous.get(parcel.id, service.NO_ADVICE)
        new = service.state_label(row.decision, row.props)
        if old != new:
            body = f"{parcel.alias}: {old} → {new}. {head}"
            _post(
                repo,
                parcel.id,
                "advice.changed",
                body,
                {**base, "old": old, "new": new},
                now,
            )
    return len(parcels)


def _post(
    repo: ScopedRepository, parcel_id: int, kind: str, body: str, payload: dict, now: datetime
) -> None:
    post_event(
        repo.session,
        farm_id=repo.farm_id,
        res_model="parcel",
        res_id=parcel_id,
        kind=kind,
        body=body,
        payload=payload,
        actor=ACTOR,
        source_app=SOURCE_APP,
        occurred_at=now,
    )
