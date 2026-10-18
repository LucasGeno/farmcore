"""Activity paging. The "Show older" cursor is the boundary group's
(time, event id), the query's own order, so actions tied at one clock time are never skipped;
Activity reads raw events in successive batches until it has enough complete groups or the
history ends, so many-parcel groups never cut the feed short or split at a batch boundary."""

from __future__ import annotations

import html
import re
from datetime import timedelta

from farmcore.core import activity
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Parcel
from farmcore.core.timeline import post_event
from tests.apps.activity.test_columns import _rows
from tests.core.test_activity import NOW, farm  # noqa: F401  (fixture)

APPS = {"core", "operations", "advisor", "share", "chat", "fields", "activity"}


def _repo(session, owner):
    return ScopedRepository(session, owner.id, ensure_owner_grant(session, owner), "t", "test")


def _grant(session, owner, n: int, at) -> None:
    post_event(
        session,
        farm_id=owner.id,
        res_model="data_grant",
        res_id=n,
        kind="share.granted",
        body=f"Granted Coop{n} read access",
        payload={},
        actor="owner",
        source_app="share",
        occurred_at=at,
    )


def _advice_group(session, owner, parcel_ids: list[int], at, new: str = "spray") -> None:
    for pid in parcel_ids:
        post_event(
            session,
            farm_id=owner.id,
            res_model="parcel",
            res_id=pid,
            kind="advice.changed",
            body="changed",
            payload={"new": new, "old": "none"},
            actor="job:advisor.evaluate",
            source_app="advisor",
            occurred_at=at,
        )


def test_show_older_keeps_actions_tied_at_one_time(client, session, farm) -> None:  # noqa: F811
    at = NOW - timedelta(hours=1)
    for n in range(55):
        _grant(session, farm, n, at)
    session.commit()
    page = client.get("/activity/").text
    first = [re.search(r"Coop(\d+)", r).group(1) for r in _rows(page)]
    assert len(first) == 50
    link = re.search(r'<a [^>]*href="([^"]*before=[^"]*)"[^>]*>\s*Show older', page)
    assert link, "no Show older link"
    older = client.get(html.unescape(link.group(1))).text
    rest = [re.search(r"Coop(\d+)", r).group(1) for r in _rows(older)]
    assert len(rest) == 5
    assert sorted(first + rest, key=int) == [str(n) for n in range(55)]


def test_many_parcel_groups_fill_the_page_past_one_raw_batch(session, farm) -> None:  # noqa: F811
    pids = [p.id for p in session.query(Parcel).filter_by(farm_id=farm.id)]
    for h in range(1, 22):  # 21 groups x 10 events = 210 raw events > one batch of 200
        _advice_group(session, farm, pids, NOW - timedelta(hours=h))
    session.commit()
    rows = activity.activity_rows(_repo(session, farm), NOW, "Europe/Paris", APPS, limit=51)
    assert len(rows) == 21
    assert all(len(r["parcels"]) == 10 for r in rows)


def test_a_group_split_by_the_raw_batch_boundary_stays_whole(session, farm) -> None:  # noqa: F811
    pids = sorted(p.id for p in session.query(Parcel).filter_by(farm_id=farm.id))
    _advice_group(session, farm, pids[:5], NOW - timedelta(minutes=1), new="wait")  # 5 events
    for h in range(1, 21):  # then 20 x 10: the group at events 196-205 straddles 200
        _advice_group(session, farm, pids, NOW - timedelta(hours=h))
    session.commit()
    rows = activity.activity_rows(_repo(session, farm), NOW, "Europe/Paris", APPS, limit=51)
    assert len(rows) == 21
    assert [len(r["parcels"]) for r in rows] == [5] + [10] * 20


def test_a_full_page_never_ends_mid_group(session, farm) -> None:  # noqa: F811
    pids = [p.id for p in session.query(Parcel).filter_by(farm_id=farm.id)]
    for h in range(1, 31):
        _advice_group(session, farm, pids, NOW - timedelta(hours=h))
    session.commit()
    repo = _repo(session, farm)
    page = activity.activity_rows(repo, NOW, "Europe/Paris", APPS, limit=21)
    assert len(page) == 21 and all(len(r["parcels"]) == 10 for r in page)
    cursor = (page[-1]["at"], page[-1]["cursor_id"])
    older = activity.activity_rows(repo, NOW, "Europe/Paris", APPS, limit=21, before=cursor)
    assert len(older) == 9 and all(len(r["parcels"]) == 10 for r in older)
