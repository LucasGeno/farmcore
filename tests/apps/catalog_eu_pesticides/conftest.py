"""EU active-substance tests read the recorded fixture subset; nothing here downloads."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def eu() -> Path:
    return FIXTURES / "active_substances.ndjson"
