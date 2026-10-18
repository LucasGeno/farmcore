"""`advisor.evaluate`: evaluate every parcel of every farm at the clock `now`.

Per farm: the weather series once (`service.load_series`), then per parcel its done sprays, one
`engine.evaluate`, one `advice` row (evaluated_at = now) and an `advice.evaluated` event on the
parcel. When the decision differs from the parcel's previous advice (`service.previous_decisions`;
missing = `none`) it also posts `advice.changed` (payload: parcel alias and name, old, new,
headline, card path). So a first evaluation that yields `none` posts no change. A done spray log
with no known product gives no cover and flags the row `spray_without_product`. Chat (16) pushes
the changes; advisor never imports chat. A farm without a weather station is skipped.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from farmcore.apps.advisor import service
from farmcore.apps.advisor.engine import evaluate as engine_evaluate
from farmcore.apps.advisor.manifest import JOB_NAME
from farmcore.apps.fields.seed import alias_number
from farmcore.apps.stations import service as stations
from farmcore.core.access import ScopedRepository, repo_for_job
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.timeline import post_event

logger = logging.getLogger(__name__)
ACTOR = f"job:{JOB_NAME}"
SOURCE_APP = "advisor"
PARCEL_SCOPE = "fields:read"


def evaluate(ctx: JobContext) -> None:
    farm_ids = ctx.session.scalars(
        select(Farm.id).where(Farm.id.in_(ctx.farm_ids))
        if ctx.farm_ids is not None
        else select(Farm.id).order_by(Farm.id)
    ).all()
    tz = ctx.settings.display_tz
    for farm_id in farm_ids:
        evaluate_farm(repo_for_job(ctx.session, farm_id, JOB_NAME), ctx.now, tz)


def evaluate_farm(repo: ScopedRepository, now: datetime, tz: str) -> int:
    """Evaluate the farm's parcels at `now`; returns the number of advice rows written."""
    if service.unavailable_reason(repo):
        return 0
    station = stations.station_for_farm(repo)
    if station is None:
        logger.info("%s: farm %s has no weather station, skipped", JOB_NAME, repo.farm_id)
        return 0
    farm = repo.get(Farm, repo.farm_id, scope=service.SCOPE)
    config = service.config_for(farm, now, tz)
    series = service.load_series(repo, station.id, now, tz, config)
    parcels = sorted(
        repo.list(Parcel, scope=PARCEL_SCOPE), key=lambda p: (alias_number(p.alias), p.alias)
    )
    previous = service.previous_decisions(repo, now)
    session = repo.session
    for parcel in parcels:
        sprays, ignored = service.load_sprays(repo, parcel.id, now)
        advice = engine_evaluate(series, sprays, config, now)
        row = service.advice_row(
            advice,
            series=series,
            config=config,
            farm_id=repo.farm_id,
            parcel_id=parcel.id,
            now=now,
            tz=tz,
            ignored_sprays=ignored,
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
        if old != row.decision:
            body = f"{parcel.alias}: {old} → {row.decision}. {head}"
            _post(
                repo,
                parcel.id,
                "advice.changed",
                body,
                {**base, "old": old, "new": row.decision},
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
