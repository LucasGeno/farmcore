"""Inventory seed: a few demo stock rows on the French demo farm, scene B only, as demo data."""

from __future__ import annotations

from sqlalchemy import select

from farmcore.apps.inventory import seed as inv_seed
from farmcore.apps.inventory.models import InventoryStock
from farmcore.core.models.canonical import Farm


def _rows(session) -> list[InventoryStock]:
    return list(session.scalars(select(InventoryStock)))


def test_no_scene_seeds_no_stock(session, inv, tmp_path):
    assert inv_seed.seed(session, scene=None, data_dir=tmp_path) == {}
    assert _rows(session) == []


def test_scene_b_seeds_demo_rows_on_the_default_farm_only(session, inv, tmp_path, monkeypatch):
    other = Farm(name="Spanish demo", props={"country": "ES", "product_ids": []})
    session.add(other)
    session.flush()

    counts = inv_seed.seed(session, scene="B", data_dir=tmp_path)

    rows = _rows(session)
    assert counts == {"inventory_stock": len(rows)}
    assert len(rows) >= 2
    assert {r.farm_id for r in rows} == {inv.farm.id}
    assert all(r.props.get("demo_data") is True for r in rows)
    assert all(r.season == 2024 for r in rows)
    assert {r.product_id for r in rows} <= {p.id for p in inv.products.values()}


def test_scene_b_seed_is_idempotent(session, inv, tmp_path):
    inv_seed.seed(session, scene="b", data_dir=tmp_path)
    first = len(_rows(session))
    inv_seed.seed(session, scene="b", data_dir=tmp_path)

    assert len(_rows(session)) == first
