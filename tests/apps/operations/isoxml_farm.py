"""ISOXML test helpers (not a `test_*.py` module): the default farm with synthetic P5 Les Granges
and P6 Peyrot (the demo parcels' names and areas, invented geometry), the product shortlist, and
the 10 Apr voice spray the machine file merges into. Import the `farm` fixture by name."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import ext_ids, logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Log, Parcel, Product
from farmcore.core.seed import ensure_default_farm

TZ = "Europe/Paris"
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
CLOCK = "2024-04-10 10:00"  # demo step 3: after the 06:00 voice log, past the machine pass
VOICE_AT = "2024-04-10 06:00"


def local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(TZ))


@dataclass
class Farm:
    repo: ScopedRepository
    parcels: dict[str, Parcel]
    products: dict[str, Product]  # by active substance


@pytest.fixture
def farm(session, settings) -> Farm:
    return make_farm(session, settings)


def make_farm(session, settings) -> Farm:
    """The default farm with P5 + P6 and the shortlist, on `session` (flushed, not committed)."""
    f = ensure_default_farm(session, settings)
    parcels = {}
    for alias, name, area in (("P5", "Les Granges", "4.07"), ("P6", "Peyrot", "2.21")):
        parcels[alias], _ = ext_ids.upsert(
            session,
            source="test-parcels",
            key=f"FR-TEST-{alias}",
            model=Parcel,
            values={
                "farm_id": f.id,
                "name": name,
                "alias": alias,
                "iacs_id": f"FR-TEST-{alias}",
                "area_ha": Decimal(area),
                "geom": SQUARE,
                "props": {},
            },
        )
    products = {p.active_substance: p for p in ops_seed.ensure_products(session).values()}
    session.flush()
    repo = ScopedRepository(session, f.id, ensure_owner_grant(session, f), "test", "test")
    return Farm(repo, parcels, products)


def voice_spray(session, farm: Farm, at: str = VOICE_AT) -> Log:
    """A done voice spray on P5 + P6, copper and sulphur 2 kg/ha (rank voice)."""
    log, merged = logs.record_operation(
        session,
        farm_id=farm.repo.farm_id,
        type="spray",
        parcel_ids=[farm.parcels["P5"].id, farm.parcels["P6"].id],
        ts_start=local(at),
        ts_end=None,
        quantities=[
            logs.QuantityInput("rate", Decimal("2"), "kg/ha", p.id, p.name)
            for p in (farm.products["copper"], farm.products["sulphur"])
        ],
        source=logs.SourceInput("voice", "test:voice:1", {}, 0.9, logs.RANK_VOICE),
        actor="test",
        occurred_at=local(at),
        name="Spray 10 Apr 2024 (voice)",
    )
    assert not merged
    return log
