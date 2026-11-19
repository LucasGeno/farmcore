"""Forecast test fixtures: the default farm, its owner repository and the committed synthetic
forecast file (two runs, no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.jobs import run_now
from farmcore.core.seed import ensure_default_farm
from tests.task_drain import drain

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
    drain(session_factory, settings, [MANIFEST])
    return run
