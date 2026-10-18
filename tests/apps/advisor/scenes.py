"""Advisor app test helpers: `farmcore seed` + `farmcore replay` on the test database.

Fixtures (`manifests`, `seeded`, `step`) are imported by `test_job.py` and `test_routes.py`.
`seeded` needs the demo datasets (IGN parcels and the St-Émilion station CSV, open data), so
tests that use it are marked `data`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import jobs, logs, registry
from farmcore.core import seed as core_seed
from farmcore.core.clock import parse_local
from farmcore.core.commands.seed import run_seeds
from farmcore.core.models.canonical import Parcel, Product

TZ = "Europe/Paris"


def local(s: str) -> datetime:
    return parse_local(s, TZ)


@pytest.fixture
def manifests(session) -> list:
    ordered = registry.discover()
    return registry.active_manifests(ordered, registry.read_states(session, ordered))


@pytest.fixture
def seeded(session, settings, monkeypatch, manifests, data_dir) -> Callable[..., None]:
    """`farmcore seed [--scene B]` on the test database, with the test settings."""
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    monkeypatch.setattr(ops_seed, "get_settings", lambda: settings)

    def run(scene: str | None = None) -> None:
        run_seeds(session, manifests, scene=scene, data_dir=data_dir)
        session.commit()

    return run


@pytest.fixture
def step(session_factory, manifests, settings) -> Callable[[str], None]:
    """`farmcore replay --to <local>`: every due job must succeed."""

    def run(to: str) -> None:
        runs = jobs.step_to(session_factory, manifests, local(to), settings)
        assert all(r.ok for r in runs), runs

    return run


def confirm_spray(session, settings, alias: str, ts: datetime, product_key: str | None) -> None:
    """A voice spray log on parcel `alias`, pending then confirmed (the chat confirm path).
    `product_key=None`: the quantity names no known product (product_id None)."""
    farm = core_seed.ensure_default_farm(session, settings)
    parcel = session.scalar(select(Parcel).where(Parcel.alias == alias))
    if product_key is None:
        product_id, label = None, "something"
    else:
        name = next(p.name for p in ops_seed.SHORTLIST if p.key == product_key)
        product = session.scalar(select(Product).where(Product.name == name))
        product_id, label = product.id, product.name
    pending = logs.create_pending(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[parcel.id],
        ts_start=ts,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", product_id, label)],
        source=logs.SourceInput("voice", f"test:{alias}:{ts:%Y%m%d%H%M}", {}, 0.9, logs.RANK_VOICE),
        actor="test",
        occurred_at=ts,
    )
    logs.confirm(session, pending.id, "test", ts)
    session.commit()
