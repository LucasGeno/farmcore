"""Stations fixtures: the demo farm and station seeded through the app seed."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from farmcore.core import seed as core_seed


@pytest.fixture
def seeded(session, settings, monkeypatch, tmp_path) -> Callable[[], dict[str, int]]:
    """Run the stations seed (core farm first, as `farmcore seed` does)."""
    from farmcore.apps.stations.seed import seed

    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)

    def run() -> dict[str, int]:
        core_seed.seed(session, scene=None, data_dir=tmp_path)
        return seed(session, scene=None, data_dir=tmp_path)

    return run
