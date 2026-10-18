"""The Today "Recent activity" output stays byte-identical while the Activity page
extends `core/activity.py`. A mixed scene (multi-parcel voice spray, machine-file merge, waiting
draft, advice changes, a grant) is rendered through `recent_activity` and `GET /activity/today`
and compared with snapshots (polling attributes added; filters change only the See all
destination to Activity)."""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.activity import recent_activity
from farmcore.core.models.canonical import Parcel
from farmcore.core.timeline import post_event
from tests.core.test_activity import NOW, _advice, _spray, farm  # noqa: F401  (fixture)

SNAP = Path(__file__).parent / "snapshots"
APPS = {"core", "operations", "advisor", "share", "chat", "fields"}


@pytest.fixture
def scene(session, farm):  # noqa: F811
    _spray(session, farm, ["P3"], NOW - timedelta(hours=5))
    _spray(session, farm, ["P3"], NOW - timedelta(hours=4), source="isoxml", actor="owner", key="t")
    _spray(session, farm, ["P2", "P10", "P1"], NOW - timedelta(hours=3))
    _spray(session, farm, ["P5"], NOW - timedelta(minutes=30), pending=True)
    for alias, new in (("P1", "spray"), ("P2", "spray"), ("P4", "wait")):
        _advice(session, farm, alias, "advice.changed", NOW - timedelta(hours=2), new=new)
    post_event(
        session,
        farm_id=farm.id,
        res_model="data_grant",
        res_id=7,
        kind="share.granted",
        body="Granted Coop read access: spray records, until 01 Jun 2024",
        payload={"grantee": "Coop"},
        actor="owner",
        source_app="share",
        occurred_at=NOW - timedelta(hours=1),
    )
    session.commit()
    return farm


UPDATE = os.environ.get("FARMCORE_SNAPSHOT_UPDATE") == "1"  # re-capture on purpose only


def _ids_to_aliases(session, owner, text: str) -> str:
    """Parcel ids differ between runs (sequences survive rollbacks): `/parcels/4` → `{P4}`."""
    aliases = {p.id: p.alias for p in session.query(Parcel).filter_by(farm_id=owner.id)}
    return re.sub(r"/parcels/(\d+)", lambda m: f"/parcels/{{{aliases[int(m.group(1))]}}}", text)


def _check(name: str, actual: str) -> None:
    path = SNAP / name
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.exists(), f"missing snapshot {path}"
    assert actual == path.read_text(encoding="utf-8")


def test_recent_activity_rows_are_unchanged(session, scene) -> None:
    repo = ScopedRepository(session, scene.id, ensure_owner_grant(session, scene), "t", "test")
    rows = recent_activity(repo, NOW, "Europe/Paris", APPS, limit=10)
    actual = json.dumps(rows, indent=1, ensure_ascii=False) + "\n"
    _check("recent_activity.json", _ids_to_aliases(session, scene, actual))


def test_today_widget_html_is_unchanged(client, session, scene) -> None:
    resp = client.get("/activity/today")
    assert resp.status_code == 200
    _check("activity_today.html", _ids_to_aliases(session, scene, resp.text))
