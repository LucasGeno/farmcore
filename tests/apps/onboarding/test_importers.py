"""File imports through HTTP: review first, then atomic farm-scoped confirmation."""

from pathlib import Path

import pytest
from sqlalchemy import select

from farmcore.apps.onboarding.importers import parsers, service
from farmcore.core.models.canonical import Parcel
from farmcore.core.models.drafts import Draft
from farmcore.core.seed import ensure_default_farm

FIXTURES = Path(__file__).parent / "fixtures"


def setup_farm(session, settings):
    farm = ensure_default_farm(session, settings)
    farm.props = {**farm.props, "country": "FR"}
    session.commit()
    return farm


def upload(client, filename="parcel.geojson", data=None, **fields):
    return client.post(
        "/onboarding/import",
        files={"file": (filename, data or (FIXTURES / filename).read_bytes())},
        data=fields,
        follow_redirects=False,
    )


def test_upload_reviews_before_confirm_and_reimport_does_not_duplicate(client, session, settings):
    farm = setup_farm(session, settings)
    response = upload(client)
    assert response.status_code == 303, response.text
    draft = session.scalar(select(Draft).where(Draft.farm_id == farm.id))
    assert draft.kind == "parcels"
    assert draft.provenance["file_name"] == "parcel.geojson"
    assert len(draft.provenance["file_sha256"]) == 64
    assert not session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()
    review = client.get(f"/onboarding/import/review?draft={draft.id}")
    assert review.status_code == 200
    assert "North field" in review.text and "BTH" in review.text
    assert draft.payload["parcels"][0]["area_ha"] == pytest.approx(0.829755, rel=0.005)
    assert upload(client).headers["location"] == response.headers["location"]
    result = client.post(
        f"/drafts/{draft.id}/confirm", data={"revision": 1}, follow_redirects=False
    )
    assert result.status_code == 303
    again = upload(client)
    assert again.status_code == 200 and "already" in again.text.lower()
    # A differently named file still matches the same geometry.
    assert (
        upload(client, "renamed.geojson", (FIXTURES / "parcel.geojson").read_bytes()).status_code
        == 200
    )
    session.expire_all()
    parcels = session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()
    assert len(parcels) == 1
    assert parcels[0].props["import_provenance"]["file_name"] == "parcel.geojson"


def archive(members):
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    out = BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


@pytest.mark.parametrize(
    "filename",
    [
        "parcel.kml",
        "parcel.kmz",
        "parcel.gpkg",
        "telepac.zip",
        "TASKDATA.XML",
        "parcel.csv",
        "vertices.csv",
    ],
)
def test_supported_files_propose_the_same_known_area(filename):
    data = None
    if filename == "telepac.zip":
        data = archive(
            {
                p.name: p.read_bytes()
                for p in FIXTURES.glob("parcel.*")
                if p.suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}
            }
        )
    elif filename == "parcel.kmz":
        data = archive({"doc.kml": (FIXTURES / "parcel.kml").read_bytes()})
    items = parsers.parse(data or (FIXTURES / filename).read_bytes(), filename)
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "North field"
    assert item["area_ha"] == pytest.approx(0.8297537684, rel=0.005)


@pytest.mark.parametrize(
    "case",
    [
        "traversal",
        "bomb",
        "count",
        "xxe",
        "wrong_crs",
        "missing_crs",
        "point",
        "oversize",
        "disguised",
    ],
)
def test_hostile_or_unusable_files_are_refused(case):
    import json

    filename = "bad.zip"
    if case == "disguised":
        filename = "bad.gpkg"
        data = (
            '<OGRVRTDataSource><OGRVRTLayer name="parcel"><SrcDataSource>'
            f"{FIXTURES / 'parcel.geojson'}</SrcDataSource>"
            "<SrcLayer>parcel</SrcLayer></OGRVRTLayer></OGRVRTDataSource>"
        ).encode()
    elif case == "traversal":
        data = archive({"../escape.geojson": (FIXTURES / "parcel.geojson").read_bytes()})
    elif case == "bomb":
        data = archive({"doc.kml": b"x" * 21_000_000})
    elif case == "count":
        data = archive({f"{i}.txt": b"x" for i in range(101)})
    elif case == "xxe":
        filename = "bad.kml"
        data = b'<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///etc/passwd">]><kml>&x;</kml>'
    elif case == "missing_crs":
        data = archive(
            {
                p.name: p.read_bytes()
                for p in FIXTURES.glob("parcel.*")
                if p.suffix in {".shp", ".shx", ".dbf"}
            }
        )
    elif case == "oversize":
        filename, data = "bad.geojson", b" " * 5_000_001
    else:
        filename = "bad.geojson"
        doc = json.loads((FIXTURES / "parcel.geojson").read_bytes())
        if case == "wrong_crs":
            doc["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
        elif case == "country":
            doc["features"][0]["geometry"]["coordinates"] = [
                [[100, 40], [100.001, 40], [100.001, 40.001], [100, 40.001], [100, 40]]
            ]
        else:
            doc["features"][0]["geometry"] = {"type": "Point", "coordinates": [2, 48]}
        data = json.dumps(doc).encode()
    with pytest.raises(ValueError):
        parsers.parse(data, filename)


@pytest.fixture
def member(client, session, settings):
    from datetime import UTC, datetime, timedelta

    from farmcore.core.identity import DUMMY_HASH, token_hash
    from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession

    farm = setup_farm(session, settings)
    owner = LocalIdentity(login="import-owner", password_hash=DUMMY_HASH)
    session.add(owner)
    session.flush()
    session.add(FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"))
    session.add(
        LocalSession(
            token_hash=token_hash("import-session"),
            identity_id=owner.id,
            csrf_hash=token_hash("import-nonce"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    session.commit()
    del client.headers["X-Admin-Token"]
    client.cookies.set("farmcore_session", "import-session")
    client.cookies.set("farmcore_csrf", "import-nonce")
    client.headers["Origin"] = "http://testserver"
    return client


def test_member_form_csrf_upload_accepts_200_realistic_boundaries(member, session):
    import json
    import math

    # First prove the ordinary browser form path, then the larger draft capacity.
    assert upload(member, csrf_token="import-nonce").status_code == 303
    features = []
    for i in range(200):
        ring = [
            [
                2 + i * 0.003 + 0.001 * math.cos(j * math.tau / 80),
                48 + 0.001 * math.sin(j * math.tau / 80),
            ]
            for j in range(80)
        ]
        ring.append(ring[0])
        features.append(
            {
                "type": "Feature",
                "properties": {"name": f"Field {i}"},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    response = upload(
        member,
        "farm.geojson",
        json.dumps({"type": "FeatureCollection", "features": features}).encode(),
        csrf_token="import-nonce",
    )
    assert response.status_code == 303, response.text[-2000:]
    draft = session.scalar(
        select(Draft).where(Draft.provenance["file_name"].astext == "farm.geojson")
    )
    assert len(draft.payload["parcels"]) == 200
    assert len(json.dumps(draft.payload)) > 64000
    assert not session.scalars(select(Parcel)).all()
    assert member.get(f"/onboarding/import/review?draft={draft.id}").status_code == 200
    # The larger allowance remains bounded through the public draft revision service.
    from farmcore.core import drafts

    oversized = {**draft.payload["parcels"][0], "messages": ["x" * 2_000_000]}
    with pytest.raises(drafts.DraftInvalid, match="too large"):
        drafts.revise(
            session,
            member.app.state.manifests,
            farm_id=draft.farm_id,
            draft_id=draft.id,
            actor=draft.actor,
            revision=1,
            payload={"parcels": [oversized]},
        )


def test_member_chunked_cap_precedes_csrf_form_parse_and_refusals_write_nothing(member, session):
    response = member.post(
        "/onboarding/import",
        content=(b"x" * 100000 for _ in range(51)),
        headers={"Content-Type": "multipart/form-data; boundary=synthetic"},
    )
    assert response.status_code == 413
    assert upload(member, csrf_token="wrong").status_code == 403
    assert (
        upload(member, "bad.csv", b"wkt\n" + b"x" * 140000, csrf_token="import-nonce").status_code
        == 422
    )
    member.cookies.clear()
    assert upload(member).status_code == 403
    assert not session.scalars(select(Draft)).all()


def test_telepac_layers_ignore_sidecars_and_allow_explicit_choice():
    members = {}
    for stem in ("ilots", "parcelles"):
        for p in FIXTURES.glob("parcel.*"):
            if p.suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                members[stem + p.suffix] = p.read_bytes()
    members["parcelles.shp.xml"] = b"<metadata/>"
    data = archive(members)
    assert parsers.parse(data, "telepac.zip")[0]["name"] == "North field"
    assert parsers.parse(data, "telepac.zip", layer="ilots.shp")[0]["name"] == "North field"


@pytest.mark.parametrize("case", ["file_directory_collision", "long_csv_field", "expanded_xml"])
def test_bad_archive_and_csv_fail_as_input_errors(case, monkeypatch):
    if case == "file_directory_collision":
        filename, data = "bad.zip", archive({"a": b"file", "a/doc.kml": b"<kml/>"})
    elif case == "long_csv_field":
        filename, data = "bad.csv", b"wkt\n" + b"x" * 140000
    else:
        monkeypatch.setattr(parsers, "MAX_XML_BYTES", 200)
        filename, data = "bad.kmz", archive({"doc.kml": (FIXTURES / "parcel.kml").read_bytes()})
    with pytest.raises(ValueError):
        parsers.parse(data, filename)


def test_excel_csv_mapping_strips_spoofed_notices_and_preserves_repair_notice():
    data = (
        "label;culture;messages;wkt\nPr\xe9;VRC;Safe to confirm;"
        '"POLYGON ((2 48,2.001 48.001,2.001 48,2 48.001,2 48))"\n'
    ).encode("cp1252")
    [item] = parsers.parse(data, "excel.csv", name_field="label", crop_field="culture")
    assert item["name"] == "Pré" and item["crop_code"] == "VRC"
    assert item["area_ha"] == pytest.approx(0.414877, rel=0.005)
    assert len(item["messages"]) == 1 and "repaired" in item["messages"][0]


def test_review_deduplicates_file_and_warns_on_redrawn_overlap(session, settings):
    import json

    from geoalchemy2.shape import from_shape
    from shapely.geometry import MultiPolygon, shape

    farm = setup_farm(session, settings)
    items = parsers.parse((FIXTURES / "parcel.geojson").read_bytes(), "parcel.geojson")
    item = items[0]
    old = Parcel(
        farm_id=farm.id,
        name="Existing field",
        alias="old",
        area_ha=0.829755,
        geom=from_shape(MultiPolygon([shape(item["geometry"])]), srid=4326),
        props={},
    )
    session.add(old)
    session.flush()
    shifted = json.loads(json.dumps(item))
    for point in shifted["geometry"]["coordinates"][0]:
        point[0] += 0.0001
    payload = service.review_items([old], [shifted, shifted])
    assert len(payload) == 1
    assert any("Existing field" in msg and "%" in msg for msg in payload[0]["messages"])
    old.props = {
        "import_source_ref": item["source_ref"],
        "import_source_scope": item["source_scope"],
    }
    assert service.review_items([old], [shifted]) == []
    shifted["source_scope"] = "another-file.geojson"
    assert len(service.review_items([old], [shifted])) == 1
    # Country rejection belongs to the service, not the format/HTTP tables.
    for point in shifted["geometry"]["coordinates"][0]:
        point[0] += 100
    with pytest.raises(ValueError, match="country"):
        service.validate(session, farm.id, {"parcels": [shifted]})


def test_imported_crop_codes_feed_reviewed_seasons_and_unknowns_stay_unset(
    client, session, settings
):
    import copy
    import json
    import re

    from farmcore.core.models.canonical import CropSeason

    farm = setup_farm(session, settings)
    doc = json.loads((FIXTURES / "parcel.geojson").read_bytes())
    unknown = copy.deepcopy(doc["features"][0])
    unknown["properties"] = {"name": "Unknown field", "crop_code": "ZZZ"}
    for point in unknown["geometry"]["coordinates"][0]:
        point[0] += 0.01
    doc["features"].append(unknown)
    assert upload(client, data=json.dumps(doc).encode()).status_code == 303
    draft = session.scalar(select(Draft).where(Draft.farm_id == farm.id))
    assert (
        client.post(
            f"/drafts/{draft.id}/confirm", data={"revision": 1}, follow_redirects=False
        ).status_code
        == 303
    )

    page = client.get("/onboarding/crops")
    assert page.status_code == 200
    parcels = session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()
    choices = {}
    for parcel in parcels:
        row = re.search(rf'<tr\b[^>]*data-parcel="{parcel.id}"[^>]*>', page.text)
        assert row is not None
        crop = re.search(r'data-crop="([^"]*)"', row.group())
        assert crop is not None
        if parcel.name == "North field":
            assert crop.group(1) == "soft-wheat"
            choices[f"crop_{parcel.id}"] = crop.group(1)
        else:
            assert parcel.name == "Unknown field"
            assert crop.group(1) == ""
            assert 'data-unknown-code="ZZZ"' in row.group()
    assert len(parcels) == 2
    assert (
        client.post(
            "/onboarding/crops/assign",
            data={"starts_on": "2024-03-01", "parcel": [str(p.id) for p in parcels], **choices},
            follow_redirects=False,
        ).status_code
        == 303
    )
    session.expire_all()
    seasons = session.scalars(select(CropSeason).where(CropSeason.farm_id == farm.id)).all()
    known = next(p for p in parcels if p.name == "North field")
    assert [(s.parcel_id, s.crop_name, s.crop_eppo) for s in seasons] == [
        (known.id, "Soft wheat", "TRZAX")
    ]
