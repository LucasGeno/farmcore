"""Forecast test fixtures: the default farm, its owner repository and the committed synthetic
forecast file (two runs, no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.core import tasks
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.jobs import run_now
from farmcore.core.seed import ensure_default_farm

FIXTURE = Path(__file__).parent / "fixtures" / "forecast-runs.json"


@pytest.fixture
def farm(session, settings):
    farm = ensure_default_farm(session, settings)
    session.flush()
    return farm


@pytest.fixture
def repo(session, farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")


def provider_client(provider):
    """Construct a real async HTTP client over the controlled external transport."""

    def create(**kwargs):
        transport = httpx.MockTransport(lambda request: provider(str(request.url), **kwargs))
        return AsyncClient(transport=transport, **kwargs)

    return create


def fetch_live_now(session_factory, settings):
    """A live forecast tick end to end, in this process: the job (as a handler or the worker's
    tick runs it) only enqueues; then every queued fetch runs as the worker would run it."""
    run = run_now(session_factory, [MANIFEST], "forecast.fetch_live", settings)
    drain(session_factory, settings)
    return run


def drain(session_factory, settings, manifests=(MANIFEST,)) -> int:
    """Claim every task queued now and run it in this process, as a worker would; how many ran.
    Test-only: claims carry no worker, so a running worker would treat them as stalled."""
    specs = {spec.name: spec for spec in tasks.durable_specs(manifests)}
    fetch = text("SELECT id, task_name, args FROM procrastinate_fetch_job_v2(:q, NULL)")
    with session_factory() as session:
        claimed = []
        while (job := session.execute(fetch, {"q": [tasks.QUEUE]}).first()).id is not None:
            claimed.append(job)
        session.commit()
    for job in claimed:
        tasks.run_task(session_factory, settings, specs[job.task_name], job.args["farm_id"])
    with session_factory() as session:
        for job in claimed:
            session.execute(
                text("SELECT procrastinate_finish_job_v1(:id, 'succeeded', false)"), {"id": job.id}
            )
        session.commit()
    return len(claimed)
