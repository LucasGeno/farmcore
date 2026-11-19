"""A farm with measured and unmeasured parcels, one fertiliser in the catalogue with 1000 kg
bought this season, and the replay clock on 10 April 2024 06:00 Paris."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.inventory.service import item_stock_rows
from farmcore.core import drafts, registry
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.models.catalog import CatalogItem
from farmcore.core.seed import ensure_default_farm

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
APPLIED = NOW.replace(day=9, hour=8)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
MANIFESTS = registry.discover()
# Calcium ammonium nitrate as its bag declares it: 27 % N.
LABEL = {"nutrients": {"N": "27"}, "source": "label", "reference": "bag, lot 24-117"}


def seed(session, settings, *, farm: Farm | None = None) -> SimpleNamespace:
    farm = farm or ensure_default_farm(session, settings)
    parcels = {
        alias: Parcel(farm_id=farm.id, alias=alias, name=name, area_ha=Decimal(area), geom=SQUARE)
        for alias, name, area in (("A", "Le Plateau", "2"), ("Z", "Unmeasured", "0"))
    }
    item = CatalogItem(name="CAN 27")
    session.add_all([*parcels.values(), item])
    session.flush()
    session.add(
        InventoryStock(
            farm_id=farm.id, catalog_item_id=item.id, season=2024, unit="kg", purchased=1000
        )
    )
    set_replay(session, NOW, settings)
    session.flush()
    return SimpleNamespace(farm_id=farm.id, item=item.id, **{k: p.id for k, p in parcels.items()})


def application(case, **changes) -> dict:
    payload = {
        "parcel_ids": [case.A],
        "ts_start": APPLIED.isoformat(),
        "fertiliser": {"name": "CAN 27", "catalog_item_id": case.item},
        "amount": {"value": "300", "unit": "kg/ha"},
        "composition": LABEL,
    }
    payload.update(changes)
    return {k: v for k, v in payload.items() if v is not None}


def propose(session, case, payload, *, kind="fertilization", key="k1", actor="operator"):
    draft, _ = drafts.propose(
        session,
        MANIFESTS,
        farm_id=case.farm_id,
        actor=actor,
        kind=kind,
        payload=payload,
        provenance={"source_type": "manual"},
        request_key=key,
    )
    return draft


def confirm(session, case, draft_id, *, revision=1, actor="operator"):
    return drafts.confirm(
        session,
        MANIFESTS,
        farm_id=case.farm_id,
        draft_id=draft_id,
        actor=actor,
        revision=revision,
        now=NOW,
    )


def used(session, case) -> Decimal | None:
    """CAN 27 used this season, as Inventory computes it."""
    farm = session.get(Farm, case.farm_id)
    repo = ScopedRepository(
        session, case.farm_id, ensure_owner_grant(session, farm), "test", "test"
    )
    (row,) = item_stock_rows(repo, now=NOW, tz="Europe/Paris")
    return row.used


@pytest.fixture
def case(session, settings):
    return seed(session, settings)
