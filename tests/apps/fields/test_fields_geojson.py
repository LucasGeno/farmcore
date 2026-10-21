"""`GET /fields/parcels.geojson`: parcels in alias order with latest advice and last spray,
both bounded by the clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from farmcore.apps.fields.seed import seed
from farmcore.core import ext_ids
from farmcore.core.clock import set_replay
from farmcore.core.commands.seed import run_seeds
from farmcore.core.models.canonical import AccessLog, Advice, Log, Parcel
from farmcore.core.registry import discover
from tests.apps.advisor.review_fixtures import complete_station

# 10 Apr 2024 06:00 Europe/Paris (CEST, UTC+2).
NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
URL = "/fields/parcels.geojson"


def _parcel(session, id_parcel: str) -> Parcel:
    parcel = ext_ids.lookup(session, "ign-rpg-2024", id_parcel)
    assert isinstance(parcel, Parcel)
    return parcel


@pytest.fixture
def seeded(session, synthetic_data_dir, settings):
    seed(session, scene=None, data_dir=synthetic_data_dir)
    complete_station(session, _parcel(session, "900010").farm_id, NOW)
    set_replay(session, NOW, settings)
    session.commit()
    return session


def _advice(session, parcel: Parcel, at: datetime, decision: str) -> None:
    session.add(
        Advice(farm_id=parcel.farm_id, parcel_id=parcel.id, evaluated_at=at, decision=decision)
    )


def _spray(session, parcel: Parcel, at: datetime, status: str = "done") -> None:
    log = Log(farm_id=parcel.farm_id, type="spray", status=status, name="spray", ts_start=at)
    log.parcels.append(parcel)
    session.add(log)


def _by_alias(body: dict) -> dict[str, dict]:
    return {f["properties"]["alias"]: f["properties"] for f in body["features"]}


def test_geojson_lists_parcels_in_alias_order_with_their_fields(client, seeded):
    resp = client.get(URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert [f["properties"]["alias"] for f in body["features"]] == ["P2", "P10"]
    ten = body["features"][1]
    parcel = _parcel(seeded, "900010")
    assert ten["type"] == "Feature"
    assert ten["id"] == parcel.id
    assert ten["geometry"]["type"] == "MultiPolygon"
    assert ten["properties"] == {
        "id": parcel.id,
        "alias": "P10",
        "name": "Synthetic Ten",
        "area_ha": 1.25,
        "variety": "Merlot",
        "decision": "none",
        "advised": False,
        "assessment_status": "unavailable",
        "last_spray": None,
        "card_url": f"/advisor/parcels/{parcel.id}",
        "label_point": ten["properties"]["label_point"],
    }
    lon, lat = ten["properties"]["label_point"]
    assert -0.150 < lon < -0.148 and 44.890 < lat < 44.892


def test_decision_is_the_latest_advice_up_to_now(client, seeded):
    two, ten = _parcel(seeded, "900002"), _parcel(seeded, "900010")
    _advice(seeded, two, NOW - timedelta(days=1), "wait")
    _advice(seeded, two, NOW - timedelta(hours=1), "spray")
    _advice(seeded, two, NOW + timedelta(hours=1), "protected")  # future of the clock
    _advice(seeded, ten, NOW + timedelta(minutes=1), "spray")  # only future advice
    seeded.commit()

    props = _by_alias(client.get(URL).json())

    assert (props["P2"]["decision"], props["P10"]["decision"]) == ("spray", "none")


def test_last_spray_is_the_latest_done_spray_up_to_now_as_local_date(client, seeded):
    two, ten = _parcel(seeded, "900002"), _parcel(seeded, "900010")
    _spray(seeded, two, datetime(2024, 4, 2, 6, 0, tzinfo=UTC))
    # 22:30 UTC on 8 Apr is 00:30 on 9 Apr in Paris: the local date counts.
    _spray(seeded, two, datetime(2024, 4, 8, 22, 30, tzinfo=UTC))
    _spray(seeded, two, NOW + timedelta(hours=2))  # future of the clock
    _spray(seeded, ten, NOW - timedelta(hours=3), status="pending")  # not done
    seeded.commit()

    props = _by_alias(client.get(URL).json())

    assert (props["P2"]["last_spray"], props["P10"]["last_spray"]) == ("2024-04-09", None)


def test_geojson_and_map_page_each_log_a_fields_read(client, seeded):
    def fields_reads() -> int:
        return seeded.scalar(
            select(func.count()).select_from(AccessLog).where(AccessLog.scope == "fields:read")
        )

    before = fields_reads()
    assert client.get(URL).status_code == 200
    after_geojson = fields_reads()
    assert client.get("/fields/").status_code == 200

    assert after_geojson >= before + 1
    assert fields_reads() >= after_geojson + 1


def test_geojson_before_any_seed_says_to_seed(client):
    resp = client.get(URL)

    assert resp.status_code == 503
    assert "farmcore seed" in resp.json()["detail"]


@pytest.mark.data
def test_geojson_on_the_real_parcels_has_p1_to_p10(client, session, data_dir, settings):
    run_seeds(
        session,
        [m for m in discover() if m.name in ("core", "fields")],
        scene=None,
        data_dir=data_dir,
    )
    set_replay(session, NOW, settings)
    p3 = session.scalar(select(Parcel).where(Parcel.alias == "P3"))
    complete_station(session, p3.farm_id, NOW)
    _advice(session, p3, NOW - timedelta(hours=1), "spray")
    _advice(session, p3, NOW + timedelta(days=1), "protected")  # future of the clock
    session.commit()

    features = client.get(URL).json()["features"]

    assert [f["properties"]["alias"] for f in features] == [f"P{i}" for i in range(1, 11)]
    for f in features:
        assert {"alias", "name", "decision", "last_spray"} <= f["properties"].keys()
    assert features[4]["properties"]["name"] == "Les Granges"
    assert [f["properties"]["decision"] for f in features] == ["none"] * 2 + ["spray"] + [
        "none"
    ] * 7


def test_map_page_has_the_map_legend_and_parcel_list(client, seeded):
    _advice(seeded, _parcel(seeded, "900002"), NOW - timedelta(hours=1), "spray")
    seeded.commit()

    resp = client.get("/fields/")

    assert resp.status_code == 200
    html = resp.text
    assert "/static/core/vendor/maplibre-gl-4.7.1.js" in html
    farm_id = _parcel(seeded, "900002").farm_id
    assert 'id="parcel-map"' in html
    assert f'data-geojson-url="/fields/parcels.geojson?farm={farm_id}"' in html
    assert '<script src="/static/fields/map.js"></script>' in html
    for label in ("Spray", "Wait", "Protected", "No advice yet"):
        assert label in html
    assert html.index("Synthetic Two") < html.index("Synthetic Ten")
    assert 'href="/fields/"' in html  # Map navbar entry


def test_map_script_is_served(client):
    resp = client.get("/static/fields/map.js")

    assert resp.status_code == 200
    assert "tile.openstreetmap.org" in resp.text and "OpenStreetMap" in resp.text
