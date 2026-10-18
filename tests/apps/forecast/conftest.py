"""Forecast test fixtures: the default farm, its owner repository and the committed synthetic
forecast file (two runs, no network)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

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


def provider_stream(provider):
    """Adapt a controlled external response to HTTPX's real streaming transport."""

    @contextmanager
    def stream(method, url, **kwargs):
        transport = httpx.MockTransport(lambda request: provider(str(request.url), **kwargs))
        with (
            httpx.Client(transport=transport) as client,
            client.stream(method, url, **kwargs) as response,
        ):
            yield response

    return stream
