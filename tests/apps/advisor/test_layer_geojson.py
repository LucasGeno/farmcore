"""`GET /advisor/layer.geojson`: the "Spray advice" map layer. One feature per parcel with
the decision of its latest advice up to the clock (`none` without advice), its label and colour
from the shell tokens, and the popup facts (cover, last spray).

Synthetic parcels (two squares, not IGN data) and hand-written advice rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from farmcore.apps.fields.seed import seed
from farmcore.core import ext_ids
from farmcore.core import seed as core_seed
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import AccessLog, Advice, Log, Parcel
from tests.apps.fields.conftest import SYNTHETIC

# 10 Apr 2024 06:00 Europe/Paris (CEST, UTC+2).
NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
URL = "/advisor/layer.geojson"


@pytest.fixture
def seeded(session, settings, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    (tmp_path / "ign").mkdir()
    (tmp_path / "ign" / "demo_parcels_named.geojson").write_text(json.dumps(SYNTHETIC))
    seed(session, scene=None, data_dir=tmp_path)
    set_replay(session, NOW, settings)
    session.commit()
    return session


def _parcel(session, id_parcel: str) -> Parcel:
    parcel = ext_ids.lookup(session, "ign-rpg-2024", id_parcel)
    assert isinstance(parcel, Parcel)
    return parcel


def _advice(session, parcel: Parcel, at: datetime, decision: str, **kw) -> None:
    session.add(
        Advice(
            farm_id=parcel.farm_id, parcel_id=parcel.id, evaluated_at=at, decision=decision, **kw
        )
    )


def _by_alias(body: dict) -> dict[str, dict]:
    return {f["properties"]["alias"]: f["properties"] for f in body["features"]}


def test_layer_has_one_feature_per_parcel_with_geometry_and_no_advice_yet(client, seeded):
    resp = client.get(URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert [f["properties"]["alias"] for f in body["features"]] == ["P2", "P10"]
    ten = body["features"][1]
    assert ten["geometry"]["type"] == "MultiPolygon"
    props = ten["properties"]
    assert props["id"] == ten["id"] == _parcel(seeded, "900010").id
    assert props["name"] == "Synthetic Ten"
    assert (props["decision"], props["advised"]) == ("none", False)
    assert (props["label"], props["colour"]) == ("No advice yet", "#4b5563")
    assert props["protected_until"] is None and props["last_spray"] is None


def test_decision_label_and_colour_follow_the_latest_advice_up_to_the_clock(client, seeded):
    two, ten = _parcel(seeded, "900002"), _parcel(seeded, "900010")
    _advice(seeded, two, NOW - timedelta(days=1), "wait")
    _advice(seeded, two, NOW - timedelta(hours=1), "spray")
    _advice(seeded, two, NOW + timedelta(hours=1), "protected")  # after the clock: ignored
    _advice(seeded, ten, NOW + timedelta(minutes=1), "spray")  # only future advice
    seeded.commit()

    props = _by_alias(client.get(URL).json())

    assert (props["P2"]["decision"], props["P2"]["advised"]) == ("spray", True)
    assert (props["P2"]["label"], props["P2"]["colour"]) == ("Spray", "#b91c1c")
    assert props["P2"]["hatch"] is True
    assert (props["P10"]["decision"], props["P10"]["advised"]) == ("none", False)
    assert props["P10"]["hatch"] is False


def test_protected_parcel_carries_its_cover_end_and_last_spray_as_local_dates(client, seeded):
    two = _parcel(seeded, "900002")
    # 22:30 UTC on 12 Apr is 00:30 on 13 Apr in Paris: the local date counts.
    until = datetime(2024, 4, 12, 22, 30, tzinfo=UTC)
    _advice(seeded, two, NOW - timedelta(hours=2), "protected", protected_until=until)
    log = Log(
        farm_id=two.farm_id,
        type="spray",
        status="done",
        name="spray",
        ts_start=datetime(2024, 4, 8, 22, 30, tzinfo=UTC),
    )
    log.parcels.append(two)
    seeded.add(log)
    seeded.commit()

    props = _by_alias(client.get(URL).json())["P2"]

    assert (props["label"], props["colour"]) == ("Protected", "#15803d")
    assert props["protected_until"] == "2024-04-13"
    assert props["last_spray"] == "2024-04-09"
    assert props["card_url"] == f"/advisor/parcels/{two.id}"


def test_layer_reads_advice_and_parcels_through_the_scoped_repo(client, seeded):
    def reads(scope: str) -> int:
        return seeded.scalar(
            select(func.count()).select_from(AccessLog).where(AccessLog.scope == scope)
        )

    before = reads("advice:read"), reads("fields:read")
    assert client.get(URL).status_code == 200

    assert reads("advice:read") > before[0] and reads("fields:read") > before[1]


def test_cover_end_is_sent_only_for_a_protected_decision(client, seeded):
    # The sulphur-only pass can turn `protected` into spray/wait and keep the downy cover in
    # `protected_until`: the popup must not say "Protected until" next to a Spray chip.
    two, ten = _parcel(seeded, "900002"), _parcel(seeded, "900010")
    until = datetime(2024, 4, 12, 22, 30, tzinfo=UTC)
    _advice(seeded, two, NOW - timedelta(hours=2), "spray", protected_until=until)
    _advice(seeded, ten, NOW - timedelta(hours=2), "wait", protected_until=until)
    seeded.commit()

    props = _by_alias(client.get(URL).json())

    assert props["P2"]["decision"] == "spray" and props["P2"]["protected_until"] is None
    assert props["P10"]["decision"] == "wait" and props["P10"]["protected_until"] is None
