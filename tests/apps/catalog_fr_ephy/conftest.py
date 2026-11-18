"""E-Phy import tests read the recorded fixture subset; nothing here downloads."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ephy() -> dict[str, Path]:
    return {
        "products": FIXTURES / "products_utf8.csv",
        "uses": FIXTURES / "uses_utf8.csv",
        "products_cp1252": FIXTURES / "products_cp1252.csv",
        "uses_cp1252": FIXTURES / "uses_cp1252.csv",
    }
