"""An explicit `?farm=` survives every map, layer, station and advisor data or drill-down URL
(`farmcore.web.context_url` / `farm_url`), so a shared link opened without the farm cookie keeps
reading the farm it names. Without `?farm=` the URLs stay bare. Scene B replayed to 10 Apr 06:00
with P5 and P6 sprayed (both decisions on the page)."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from farmcore.core import jobs
from farmcore.core.access import default_farm
from farmcore.core.models.canonical import Asset, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local

pytestmark = pytest.mark.data

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

INTERNAL = re.compile(
    r'(?:href|hx-get|data-geojson-url)="(/(?:fields|advisor|stations|operations)/[^"]*)"'
)


@pytest.fixture
def farm_id(session, session_factory, settings, manifests, seeded, step) -> int:
    seeded("B")
    step("2024-04-08 06:00")
    step("2024-04-10 06:00")
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 05:30"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok
    return default_farm(session, settings).id


def _links(page: str) -> list[str]:
    return [html.unescape(u) for u in INTERNAL.findall(page)]


def _with(url: str, farm_id: int) -> bool:
    return re.search(rf"[?&]farm={farm_id}(?:&|#|$)", url) is not None


def _pages(session, farm_id: int) -> list[str]:
    parcel = session.scalar(
        select(Parcel.id).where(Parcel.farm_id == farm_id, Parcel.alias == "P5")
    )
    station = session.scalar(
        select(Asset.id).where(Asset.farm_id == farm_id, Asset.kind == "station")
    )
    return [
        "/fields/",
        "/fields/today",
        "/advisor/",
        "/advisor/today",
        f"/advisor/parcels/{parcel}",
        "/stations/",
        f"/stations/{station}",
        "/stations/today",
    ]


def test_every_internal_link_on_the_app_pages_keeps_the_farm(client, session, farm_id):
    for path in _pages(session, farm_id):
        page = client.get(path, params={"farm": farm_id})
        assert page.status_code == 200, path
        links = _links(page.text)
        assert links, path
        bare = [u for u in links if not _with(u, farm_id)]
        assert bare == [], f"{path}: {bare}"


def test_cookie_only_reading_links_freeze_context_only_on_map_and_legacy_detail(
    client, session, farm_id
):
    for path in _pages(session, farm_id):
        links = _links(client.get(path).text)
        if path in ("/fields/", "/fields/today"):
            scoped = [u for u in links if "farm=" in u]
            assert scoped and all(_with(u, farm_id) for u in scoped)
            if path == "/fields/":
                assert any("return_to=" in u for u in scoped)
        elif path.startswith("/advisor/parcels/"):
            fallback = f"/advisor/?farm={farm_id}"
            assert fallback in links
            assert all(_with(u, farm_id) for u in links if "farm=" in u), path
        elif path.startswith("/advisor/"):
            scoped = [u for u in links if "farm=" in u]
            assert all(_with(u, farm_id) for u in scoped), path
            assert any(urlsplit(u).path.startswith("/fields/parcels/") for u in scoped), path
        else:
            assert not any("farm=" in u for u in links), path


def test_map_layer_urls_and_feature_links_keep_the_farm(client, farm_id):
    page = client.get("/fields/", params={"farm": farm_id}).text
    layers = json.loads(html.unescape(re.search(r"data-layers='([^']*)'", page).group(1)))
    assert layers and all(_with(layer["url"], farm_id) for layer in layers)

    for url in ("/fields/parcels.geojson", "/advisor/layer.geojson"):
        features = client.get(url, params={"farm": farm_id}).json()["features"]
        assert features and all(_with(f["properties"]["card_url"], farm_id) for f in features)
    [station] = client.get("/stations/layer.geojson", params={"farm": farm_id}).json()["features"]
    assert _with(station["properties"]["url"], farm_id)


def test_feature_links_stay_bare_without_a_farm_query(client, farm_id):
    features = client.get("/advisor/layer.geojson").json()["features"]
    assert all("farm=" not in f["properties"]["card_url"] for f in features)


@pytest.mark.spanish_data
def test_each_farms_station_links_and_archive_resolve_without_cross_farm_leaks(
    client, session, farm_id
):
    """Follow actual list/detail links, including VA07's separate source archive."""
    stations = session.scalars(select(Asset).where(Asset.kind == "station")).all()
    assert len(stations) == 2
    for station in stations:
        page = client.get("/stations/", params={"farm": station.farm_id})
        links = _links(page.text)
        detail = f"/stations/{station.id}?farm={station.farm_id}"
        assert detail in links
        assert not any(
            f"/stations/{other.id}?" in page.text for other in stations if other.id != station.id
        )
        response = client.get(detail)
        assert response.status_code == 200
        if station.props.get("archive_only"):
            archive = re.search(r'href="(/demo_es/archive/[^\"]+)"', response.text)
            assert archive
            assert client.get(html.unescape(archive[1])).status_code == 200
            assert (
                client.get(f"/stations/{station.id}", params={"farm": farm_id}).status_code == 404
            )
