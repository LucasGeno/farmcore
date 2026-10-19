"""KPI test fixtures: a client whose `data_dir` is `tmp_path` (synthetic station file)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from farmcore.core import seed as core_seed
from farmcore.main import create_app


@pytest.fixture(autouse=True)
def _seed_settings(monkeypatch, settings):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)


@pytest.fixture
def kpi_settings(settings, tmp_path):
    return settings.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def kpi_client(kpi_settings, session_factory) -> Iterator[TestClient]:
    app = create_app(kpi_settings, session_factory=session_factory)
    with TestClient(
        app, headers={"X-Admin-Token": kpi_settings.admin_token.get_secret_value()}
    ) as c:
        yield c
