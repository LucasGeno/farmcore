"""`core.activity.activity_rows`, the Activity page's rows. The same grouped rows as
Today's feed, plus the raw fields the page's columns need (time, actor, parcels, products,
source types), with paging by a `before` cursor (time, event id). `core.actors.mask_actor` is
the shared masking rule."""

from __future__ import annotations

from datetime import timedelta

from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.activity import activity_rows
from farmcore.core.timeline import post_event
from tests.core.test_activity import NOW, _spray, farm  # noqa: F401  (fixture)

APPS = {"core", "operations", "advisor", "share", "chat", "fields"}


def _repo(session, farm):  # noqa: F811
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "t", "test")


def test_rows_carry_time_actor_parcels_products_and_sources(session, farm) -> None:  # noqa: F811
    at = NOW - timedelta(hours=1)
    _spray(session, farm, ["P2", "P1"], at, actor="chat:123456789")
    session.commit()
    (row,) = activity_rows(_repo(session, farm), NOW, "Europe/Paris", APPS)
    assert row["text"] == "Spray logged on P1, P2"
    assert row["at"] == at
    assert row["actor"] == "chat:123456789"
    assert row["parcels"] == ["P1", "P2"]
    assert row["products"] == ["copper 2 kg/ha"]
    assert row["source_types"] == ["voice"]


def test_share_rows_have_no_parcels_or_products(session, farm) -> None:  # noqa: F811
    post_event(
        session,
        farm_id=farm.id,
        res_model="data_grant",
        res_id=3,
        kind="share.revoked",
        body="Revoked Coop's access",
        payload={},
        actor="owner",
        source_app="share",
        occurred_at=NOW - timedelta(minutes=5),
    )
    session.commit()
    (row,) = activity_rows(_repo(session, farm), NOW, "Europe/Paris", APPS)
    assert (row["actor"], row["parcels"], row["products"], row["source_types"]) == (
        "owner",
        [],
        [],
        [],
    )


def test_more_than_five_rows_and_paging_before_a_time(session, farm) -> None:  # noqa: F811
    for h in range(1, 9):
        _spray(session, farm, [f"P{h}"], NOW - timedelta(hours=h))
    session.commit()
    repo = _repo(session, farm)
    rows = activity_rows(repo, NOW, "Europe/Paris", APPS, limit=6)
    assert [r["parcels"] for r in rows] == [[f"P{h}"] for h in range(1, 7)]
    cursor = (rows[-1]["at"], rows[-1]["cursor_id"])
    older = activity_rows(repo, NOW, "Europe/Paris", APPS, limit=6, before=cursor)
    assert [r["parcels"] for r in older] == [["P7"], ["P8"]]


def test_mask_actor_lives_in_core_with_the_share_rule() -> None:
    from farmcore.core.actors import mask_actor

    assert mask_actor("chat:123456789") == "chat …6789"
    assert mask_actor("tg:123456789:55") == "chat …6789"
    assert mask_actor("chat:12") == "chat …"
    assert mask_actor("grant:12") == "grant:12"
    assert mask_actor("owner") == "owner"
    assert mask_actor("cli:x 1234567") == "cli:x …4567"
