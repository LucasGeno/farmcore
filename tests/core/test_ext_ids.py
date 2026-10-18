"""ext_id service: sources, lookup, idempotent upsert."""

from __future__ import annotations

from sqlalchemy import func, select

from farmcore.core import ext_ids
from farmcore.core.models.canonical import ExtId, Farm, Source


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_upsert_twice_same_key_keeps_one_row_with_updated_values(session):
    first, created_first = ext_ids.upsert(
        session, source="test-src", key="k1", model=Farm, values={"name": "Old name"}
    )
    second, created_second = ext_ids.upsert(
        session, source="test-src", key="k1", model=Farm, values={"name": "New name"}
    )

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert _count(session, Farm) == 1
    assert _count(session, ExtId) == 1
    assert session.get(Farm, first.id).name == "New name"


def test_different_keys_make_different_rows(session):
    a, _ = ext_ids.upsert(session, source="test-src", key="a", model=Farm, values={"name": "A"})
    b, _ = ext_ids.upsert(session, source="test-src", key="b", model=Farm, values={"name": "B"})
    other, _ = ext_ids.upsert(session, source="other", key="a", model=Farm, values={"name": "C"})

    assert len({a.id, b.id, other.id}) == 3


def test_lookup_returns_the_mapped_row_or_none(session):
    farm, _ = ext_ids.upsert(session, source="test-src", key="k1", model=Farm, values={"name": "F"})

    assert ext_ids.lookup(session, "test-src", "k1") is farm
    assert ext_ids.lookup(session, "test-src", "missing") is None
    assert ext_ids.lookup(session, "other-src", "k1") is None


def test_upsert_recreates_a_row_whose_target_was_deleted(session):
    farm, _ = ext_ids.upsert(session, source="test-src", key="k1", model=Farm, values={"name": "F"})
    session.delete(farm)
    session.flush()

    again, created = ext_ids.upsert(
        session, source="test-src", key="k1", model=Farm, values={"name": "F2"}
    )

    assert created is True
    assert ext_ids.lookup(session, "test-src", "k1") is again
    assert _count(session, ExtId) == 1


def test_ensure_source_is_get_or_create(session):
    s1 = ext_ids.ensure_source(session, "meteofrance", "Météo-France", "station", "Etalab-2.0")
    s2 = ext_ids.ensure_source(session, "meteofrance", "Météo-France", "station", "Etalab-2.0")

    assert s1.id == s2.id
    assert _count(session, Source) == 1
    assert (s2.code, s2.name, s2.kind, s2.licence) == (
        "meteofrance",
        "Météo-France",
        "station",
        "Etalab-2.0",
    )
