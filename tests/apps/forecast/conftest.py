"""Forecast test fixtures: the default farm, its owner repository and the committed synthetic
forecast file (two runs, no network)."""

from __future__ import annotations

from pathlib import Path

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
