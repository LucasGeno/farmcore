"""HTTP contracts for decorative, farm-scoped parcel outlines (no imagery requests)."""

import xml.etree.ElementTree as ET

import pytest

from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.core.seed import ensure_default_farm
from tests.apps.operations.conftest import make_parcel


@pytest.fixture
def outline_parcel(session, settings, client):
    farm = ensure_default_farm(session, settings)
    parcel = make_parcel(session, farm, 101, area="2", props={})
    # At the equator this two-degree wide / one-degree tall rectangle is 2:1.
    parcel.geom = "SRID=4326;MULTIPOLYGON(((0 0,2 0,2 1,0 1,0 0)))"
    session.commit()
    client.cookies.set(FARM_COOKIE, str(farm.id), domain="testserver.local")
    return farm, parcel


def test_outline_preserves_proportions_and_north_up(client, outline_parcel):
    _, parcel = outline_parcel
    response = client.get(f"/fields/parcels/{parcel.id}/outline.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    svg = ET.fromstring(response.text)
    assert svg.attrib["viewBox"] == "0 0 100 100"
    path = svg.find("{http://www.w3.org/2000/svg}path")
    # 10 units padding, 80x40 extent. Southern edge below the northern edge.
    assert path.attrib["d"] == "M10,70 L90,70 L90,30 L10,30 L10,70 Z"
    assert path.attrib["fill-rule"] == "evenodd"
    assert "private" in response.headers["cache-control"]
    assert not svg.findall(".//{http://www.w3.org/2000/svg}image")


@pytest.mark.parametrize("geometry", ["MULTIPOLYGON EMPTY", "MULTIPOLYGON(((0 0,0 0,0 0,0 0)))"])
def test_empty_geometry_keeps_a_neutral_placeholder(client, outline_parcel, session, geometry):
    _, parcel = outline_parcel
    parcel.geom = "SRID=4326;" + geometry
    session.commit()
    response = client.get(f"/fields/parcels/{parcel.id}/outline.svg")
    assert response.status_code == 200
    svg = ET.fromstring(response.text)
    assert svg.attrib["data-boundary"] == "missing"
    assert svg.find("{http://www.w3.org/2000/svg}path") is None
    assert "—" in response.text


def test_multipart_outline_keeps_islands_and_holes(client, outline_parcel, session):
    _, parcel = outline_parcel
    parcel.geom = (
        "SRID=4326;MULTIPOLYGON(((0 0,2 0,2 1,0 1,0 0),"
        "(0.5 0.2,1.5 0.2,1.5 0.8,0.5 0.8,0.5 0.2)),"
        "((3 0,4 0,4 1,3 1,3 0)))"
    )
    session.commit()
    svg = ET.fromstring(client.get(f"/fields/parcels/{parcel.id}/outline.svg").text)
    path = svg.find("{http://www.w3.org/2000/svg}path")
    assert path.attrib["d"].count("M") == 3  # exterior, hole, detached island
    assert path.attrib["fill-rule"] == "evenodd"
    assert "L90,40" in path.attrib["d"]  # eastern island is retained


def test_outline_cannot_read_a_different_farm(client, outline_parcel, session):
    _, mine = outline_parcel
    other = Farm(name="Other outline farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    theirs = make_parcel(session, other, 202, area="1", props={})
    session.commit()
    assert client.get(f"/fields/parcels/{theirs.id}/outline.svg").status_code == 404
    allowed = client.get(f"/fields/parcels/{theirs.id}/outline.svg?farm={other.id}")
    assert allowed.status_code == 200
    assert "private" in allowed.headers["cache-control"]
    assert client.get(f"/fields/parcels/{mine.id}/outline.svg?farm={other.id}").status_code == 404


def test_outline_uses_sunlight_palette(client, outline_parcel):
    _, parcel = outline_parcel
    client.cookies.set("sunlight", "1")
    response = client.get(f"/fields/parcels/{parcel.id}/outline.svg")
    assert 'stroke="rgb(20 83 45)"' in response.text
    assert response.headers["vary"] == "Cookie"


def test_outline_placement_retains_farm_context_and_names(client, outline_parcel):
    farm, parcel = outline_parcel
    path = f"/fields/parcels/{parcel.id}/outline.svg?farm={farm.id}"
    listing = client.get(f"/fields/list?farm={farm.id}")
    assert listing.status_code == 200
    assert f'src="{path}"' in listing.text
    assert 'data-size="inline"' in listing.text
    assert parcel.name in listing.text and parcel.alias in listing.text
    detail = client.get(f"/operations/parcels/{parcel.id}?farm={farm.id}")
    assert detail.status_code == 200
    assert f'src="{path}"' in detail.text
    assert 'data-size="large"' in detail.text
    assert 'alt=""' in detail.text and 'aria-hidden="true"' in detail.text


def test_lazy_outline_stays_with_source_farm_after_cookie_switch(client, outline_parcel, session):
    source, parcel = outline_parcel
    listing = client.get("/fields/list")
    image_url = f"/fields/parcels/{parcel.id}/outline.svg?farm={source.id}"
    assert f'src="{image_url}"' in listing.text
    other = Farm(name="Another tab's farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    client.cookies.set(FARM_COOKIE, str(other.id), domain="testserver.local")
    assert client.get(image_url).status_code == 200


def test_aerial_fragment_is_scoped_and_uses_bounded_lazy_tiles(client, outline_parcel):
    farm, parcel = outline_parcel
    response = client.get(
        f"/fields/parcels/{parcel.id}/aerial?farm={farm.id}", headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert "data-parcel-aerial" in response.text
    assert 'viewBox="0 0 600 400"' in response.text
    assert "data-aerial-tile" in response.text
    assert 'data-src="https://data.geopf.fr/wmts?' in response.text
    assert 'href="https://data.geopf.fr' not in response.text  # script opts in to each request
    assert response.text.count("data-aerial-tile") <= 12
    assert "© IGN" in response.text and "not live" in response.text
    assert "private" in response.headers["cache-control"]


def test_parcel_detail_offers_aerial_only_after_opt_in(client, outline_parcel):
    farm, parcel = outline_parcel
    page = client.get(f"/operations/parcels/{parcel.id}").text
    assert "data-aerial-disclosure" in page
    assert "Aerial view" in page
    assert f'"/fields/parcels/{parcel.id}/aerial"' in page  # no farm query: link stays bare
    explicit = client.get(f"/operations/parcels/{parcel.id}", params={"farm": farm.id}).text
    assert f"/fields/parcels/{parcel.id}/aerial?farm={farm.id}" in explicit
    assert "data.geopf.fr" not in page
    assert "<details open" not in page
    listing = client.get("/fields/list").text
    assert "data-aerial-disclosure" not in listing and "data.geopf.fr" not in listing


def test_aerial_cannot_read_a_different_farm(client, outline_parcel, session):
    source, parcel = outline_parcel
    other = Farm(name="Other aerial farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    theirs = make_parcel(session, other, 202, area="1", props={})
    session.commit()
    assert client.get(f"/fields/parcels/{theirs.id}/aerial").status_code == 404
    assert client.get(f"/fields/parcels/{parcel.id}/aerial?farm={other.id}").status_code == 404
    page = client.get(f"/fields/parcels/{parcel.id}/aerial?farm={source.id}")
    assert page.status_code == 200 and "<!doctype html>" in page.text
    assert "/static/fields/aerial.js" in page.text


def test_synthetic_layout_has_no_aerial_tile_requests(client, outline_parcel, session):
    farm, parcel = outline_parcel
    farm.props = {"layout": "synthetic"}
    session.commit()
    response = client.get(f"/fields/parcels/{parcel.id}/aerial", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "synthetic parcel layout" in response.text
    assert "data.geopf.fr" not in response.text and "data-aerial-tile" not in response.text
    assert f"/fields/parcels/{parcel.id}/outline.svg?farm={farm.id}" in response.text


def test_empty_boundary_has_no_aerial_tile_requests(client, outline_parcel, session):
    _, parcel = outline_parcel
    parcel.geom = "SRID=4326;MULTIPOLYGON EMPTY"
    session.commit()
    response = client.get(f"/fields/parcels/{parcel.id}/aerial", headers={"HX-Request": "true"})
    assert response.status_code == 200 and "No usable boundary" in response.text
    assert "data.geopf.fr" not in response.text


def test_workspace_reuses_one_shape_and_opt_in_aerial_without_provider_identity(
    client, outline_parcel
):
    farm, parcel = outline_parcel
    listing = client.get("/fields/", params={"farm": farm.id, "view": "list"}).text
    shape = f"/fields/parcels/{parcel.id}/outline.svg?farm={farm.id}"
    assert shape in listing
    for view in ("overview", "protection", "history", "activity"):
        workspace = client.get(
            f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "view": view}
        ).text
        assert workspace.count('id="parcel-heading"') == 1
        assert workspace.count(shape) == 1
        assert workspace.count("data-aerial-disclosure") == 1
    protection = client.get(
        f"/advisor/parcels/{parcel.id}/workspace", params={"farm": farm.id}
    ).text
    assert "data-aerial-disclosure" not in protection and "fc-parcel-outline" not in protection


def test_closed_workspace_aerial_is_idle_while_initial_activity_slot_remains_busy(
    client, outline_parcel
):
    import re

    farm, parcel = outline_parcel
    body = client.get(
        f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "view": "activity"}
    ).text
    aerial = re.search(r"<details data-aerial-disclosure.*?</details>", body, re.S)[0]
    initial = re.search(r"<div[^>]*data-fragment-body[^>]*>", aerial)[0]
    assert 'aria-busy="false"' in initial
    assert "toggle[event.target.open]" in initial
    activity = next(
        tag
        for tag in re.findall(r"<div[^>]*data-fragment-body[^>]*>", body)
        if "/activity/parcels/" in tag
    )
    assert 'aria-busy="true"' in activity


MAX_ID = 2**63 - 1


@pytest.mark.parametrize("path", ["outline.svg", "aerial"])
def test_parcel_ids_beyond_bigint_are_422_not_500(client, outline_parcel, path):
    for bad in (2**63, 0, -1):
        assert client.get(f"/fields/parcels/{bad}/{path}").status_code == 422, bad
    assert client.get(f"/fields/parcels/{MAX_ID}/{path}").status_code == 404


def _audit_rows(session) -> int:
    from sqlalchemy import func, select

    from farmcore.core.models.canonical import AccessLog

    session.expire_all()
    return session.scalar(select(func.count()).select_from(AccessLog))


def test_outline_is_cached_with_an_etag_and_writes_no_audit_row(client, outline_parcel, session):
    _, parcel = outline_parcel
    url = f"/fields/parcels/{parcel.id}/outline.svg"
    before = _audit_rows(session)

    first = client.get(url)

    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=300"
    etag = first.headers["etag"]
    assert str(parcel.id) in etag
    assert "Cookie" in first.headers["vary"]
    again = client.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag
    client.cookies.set("sunlight", "1")
    sunlit = client.get(url, headers={"If-None-Match": etag})
    assert sunlit.status_code == 200 and sunlit.headers["etag"] != etag  # palette in the key
    assert _audit_rows(session) == before


def test_outline_etag_does_not_bypass_farm_scoping(client, outline_parcel, session):
    farm, mine = outline_parcel
    etag = client.get(f"/fields/parcels/{mine.id}/outline.svg").headers["etag"]
    other = Farm(name="ETag other farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()

    r = client.get(
        f"/fields/parcels/{mine.id}/outline.svg?farm={other.id}", headers={"If-None-Match": etag}
    )

    assert r.status_code == 404


def test_grantee_and_tour_see_nothing_new(client, outline_parcel, session):
    from datetime import UTC, datetime, timedelta

    from farmcore.apps.share import service
    from farmcore.core.access import OWNER, ScopedRepository

    farm, parcel = outline_parcel
    owner = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), OWNER, "test")
    grant = service.create_grant(
        owner,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="outline check",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    grantee_page = client.get(f"/share/as/{grant.id}/").text
    assert "outline.svg" not in grantee_page and "/aerial" not in grantee_page
    assert client.get(f"/share/as/{grant.id}/parcels/{parcel.id}/outline.svg").status_code == 404

    client.cookies.set("farmcore_tour", "1")
    toured = client.get(f"/fields/parcels/{parcel.id}/outline.svg")
    assert toured.status_code == 200 and "<image" not in toured.text
    assert client.post(f"/fields/parcels/{parcel.id}/outline.svg").status_code in (403, 405)
