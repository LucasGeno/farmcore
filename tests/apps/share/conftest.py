"""Share test fixtures: the default farm with 2 synthetic parcels, the products, a done spray, a
second farm, and clients with and without the admin token.

Values are synthetic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import ext_ids, logs
from farmcore.core.access import OWNER, ScopedRepository, ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Log, Parcel, Product
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.share.synth import NOW, T_SPRAY, VOICE

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
TEST_SOURCE = "test-share-parcels"
TOKEN = "tok-share"


@dataclass
class ShareFarm:
    farm: Farm
    parcels: list[Parcel]
    products: dict[str, Product]


def make_parcel(session, farm: Farm, n: int, *, area: str = "1.00") -> Parcel:
    parcel, _ = ext_ids.upsert(
        session,
        source=TEST_SOURCE,
        key=f"FR-SHARE-{farm.id}-{n}",
        model=Parcel,
        values={
            "farm_id": farm.id,
            "name": f"Share parcel {n}",
            "alias": f"P{n}",
            "iacs_id": f"FR-SHARE-{n}",
            "area_ha": Decimal(area),
            "geom": SQUARE,
            "props": {"crop_label": "Vigne", "eppo_crop": "VITVI"},
        },
    )
    return parcel


@pytest.fixture
def demo(session, settings, monkeypatch) -> ShareFarm:
    """The default farm, 2 parcels and the product catalogue; replay clock at `NOW`."""
    monkeypatch.setattr(ops_seed, "get_settings", lambda: settings)
    farm = ensure_default_farm(session, settings)
    parcels = [make_parcel(session, farm, 1, area="1.25"), make_parcel(session, farm, 2)]
    products = ops_seed.ensure_products(session)
    set_replay(session, NOW, settings)
    session.commit()
    return ShareFarm(farm, parcels, products)


@pytest.fixture
def other_farm(session) -> Farm:
    farm = Farm(name="Second share farm")
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    make_parcel(session, farm, 1)
    session.commit()
    return farm


@pytest.fixture
def owner_repo(session, demo) -> ScopedRepository:
    return ScopedRepository(
        session, demo.farm.id, ensure_owner_grant(session, demo.farm), OWNER, "test"
    )


@pytest.fixture
def spray(session, demo) -> Callable[..., Log]:
    """Write a spray log on the demo farm (done by default)."""

    def write(
        *,
        ts: datetime = T_SPRAY,
        status: str = "done",
        doses: tuple[tuple[str, str, str], ...] = (("copper-contact", "2", "kg/ha"),),
        parcels: list[Parcel] | None = None,
        ts_end: datetime | None = None,
        source: logs.SourceInput = VOICE,
    ) -> Log:
        quantities = [
            logs.QuantityInput("rate", Decimal(dose), unit, product_id=demo.products[key].id)
            for key, dose, unit in doses
        ]
        result = logs.record_operation(
            session,
            farm_id=demo.farm.id,
            type="spray",
            parcel_ids=[p.id for p in (parcels or demo.parcels)],
            ts_start=ts,
            ts_end=ts_end,
            quantities=quantities,
            source=source,
            actor=OWNER,
            occurred_at=ts,
            status=status,
        )
        session.commit()
        return result[0]

    return write


@pytest.fixture
def make_client(settings, session_factory, demo) -> Iterator[Callable[..., TestClient]]:
    """`make_client(admin=True|False)`: the real app with an admin token configured; the admin
    client sends it as a cookie. Anonymous reads expose only the designated synthetic
    demo farm; private-mode denial is covered separately in test_private_access."""
    clients: list[TestClient] = []
    app = create_app(
        settings.model_copy(
            update={
                "admin_token": SecretStr(TOKEN),
                "demo_mode": True,
                "demo_farm_ids": [demo.farm.id],
            }
        ),
        session_factory=session_factory,
    )

    def make(*, admin: bool) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        if admin:
            c.cookies.set("admin_token", TOKEN)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def admin(make_client) -> TestClient:
    return make_client(admin=True)


@pytest.fixture
def anonymous(make_client) -> TestClient:
    return make_client(admin=False)
