"""Forecast test fixtures: the default farm, its owner repository and the committed synthetic
forecast file (two runs, no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from farmcore.core.access import ScopedRepository, ensure_owner_grant
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
