"""Operations test fixtures: the default farm with 2 synthetic parcels (ORM + ext_ids).

Never imports `fields.seed`; values are synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import ext_ids
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.seed import ensure_default_farm

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
TEST_SOURCE = "test-parcels"


@dataclass
class DemoFarm:
    farm: Farm
    parcels: list[Parcel]


def make_parcel(session, farm: Farm, n: int, *, area: str, props: dict) -> Parcel:
    parcel, _ = ext_ids.upsert(
        session,
        source=TEST_SOURCE,
        key=f"FR-TEST-{n}",
        model=Parcel,
        values={
            "farm_id": farm.id,
            "name": f"Test parcel {n}",
            "alias": f"P{n}",
            "iacs_id": f"FR-TEST-{n}",
            "area_ha": Decimal(area),
            "geom": SQUARE,
            "props": props,
        },
    )
    return parcel


@pytest.fixture
def demo(session, settings, monkeypatch) -> DemoFarm:
    """The default farm and 2 parcels; the operations seed sees the test settings."""
    monkeypatch.setattr(ops_seed, "get_settings", lambda: settings)
    farm = ensure_default_farm(session, settings)
    parcels = [
        make_parcel(
            session,
            farm,
            1,
            area="1.25",
            props={"crop_label": "Vigne", "eppo_crop": "VITVI", "variety": "Merlot"},
        ),
        make_parcel(session, farm, 2, area="0.80", props={"variety": "Cabernet franc"}),
    ]
    session.flush()
    return DemoFarm(farm, parcels)


@pytest.fixture
def add_parcel(session, demo):
    """Add parcel `n` (alias `Pn`) to the demo farm."""

    def add(n: int, *, area: str = "1.00", props: dict | None = None) -> Parcel:
        parcel = make_parcel(session, demo.farm, n, area=area, props=props or {})
        session.flush()
        return parcel

    return add
