"""Core seed: the demo farm, its owner grant and its ext_id, idempotently."""

from __future__ import annotations

from sqlalchemy import func, select

from farmcore.core import ext_ids
from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import DataGrant, ExtId, Farm


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_ensure_default_farm_twice_makes_one_farm_grant_and_ext_id(session, settings):
    first = core_seed.ensure_default_farm(session, settings)
    second = core_seed.ensure_default_farm(session, settings)

    assert first.id == second.id
    assert first.name == "Château demo"
    assert (_count(session, Farm), _count(session, DataGrant), _count(session, ExtId)) == (1, 1, 1)
    grant = session.scalar(select(DataGrant))
    assert (grant.farm_id, grant.grantee, grant.scopes) == (first.id, "owner", ["*"])
    assert ext_ids.lookup(session, "farmcore-demo", "chateau") is first


def test_core_manifest_seed_creates_the_default_farm(session, settings, monkeypatch, tmp_path):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)

    counts = core_seed.seed(session, scene=None, data_dir=tmp_path)
    core_seed.seed(session, scene="B", data_dir=tmp_path)

    assert counts == {"farm": 1}
    assert _count(session, Farm) == 1
    assert isinstance(ext_ids.lookup(session, "farmcore-demo", settings.default_farm_key), Farm)
