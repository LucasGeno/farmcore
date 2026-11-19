"""File imports through HTTP: review first, then atomic farm-scoped confirmation."""

from pathlib import Path

import pytest
from sqlalchemy import select

from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Farm, Parcel
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
    other = Farm(name="Other farm", props={"country": "FR"})
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    assert (
        client.post(f"/drafts/{draft.id}/confirm?farm={other.id}", data={"revision": 1}).status_code
        == 404
    )
    for _ in range(2):
        result = client.post(
            f"/drafts/{draft.id}/confirm", data={"revision": 1}, follow_redirects=False
        )
        assert result.status_code == 303, result.text
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
def test_supported_files_propose_the_same_known_area(client, session, settings, filename):
    setup_farm(session, settings)
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
    response = upload(client, filename, data)
    assert response.status_code == 303, response.text
    draft = session.scalar(select(Draft))
    assert len(draft.payload["parcels"]) == 1
    item = draft.payload["parcels"][0]
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
        "country",
        "point",
        "oversize",
        "disguised",
    ],
)
def test_hostile_or_unusable_files_leave_no_draft_or_parcel(client, session, settings, case):
    import json

    setup_farm(session, settings)
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
    response = upload(client, filename, data)
    assert response.status_code == 422
    assert not session.scalars(select(Draft)).all()
    assert not session.scalars(select(Parcel)).all()


def test_repaired_geometry_and_expert_mapping_are_visible_in_review(client, session, settings):
    import json

    setup_farm(session, settings)
    doc = json.loads((FIXTURES / "parcel.geojson").read_bytes())
    f = doc["features"][0]
    f["properties"] = {"label": "Repaired field", "culture": "VRC"}
    f["geometry"]["coordinates"] = [[[2, 48], [2.001, 48.001], [2.001, 48], [2, 48.001], [2, 48]]]
    response = upload(
        client,
        "crossed.geojson",
        json.dumps(doc).encode(),
        name_field="label",
        crop_field="culture",
    )
    assert response.status_code == 303
    draft = session.scalar(select(Draft))
    review = client.get(f"/onboarding/import/review?draft={draft.id}")
    assert "Invalid boundary repaired" in review.text and "VRC" in review.text
    assert "Repaired field" in review.text
    result = client.post(
        f"/drafts/{draft.id}/confirm", data={"revision": 1}, follow_redirects=False
    )
    assert result.status_code == 303
    parcel = session.scalar(select(Parcel))
    assert float(parcel.area_ha) == pytest.approx(0.414877, rel=0.005)


def test_repeated_upload_reuses_open_draft_and_distinct_drafts_confirm_once(
    client, session, settings
):
    setup_farm(session, settings)
    first = upload(client)
    second = upload(client)
    assert first.headers["location"] == second.headers["location"]
    assert len(session.scalars(select(Draft)).all()) == 1
    # Different file receipts can overlap. Confirm rechecks after another draft writes.
    third = upload(client, "another.geojson", (FIXTURES / "parcel.geojson").read_bytes())
    for url in (first.headers["location"], third.headers["location"]):
        assert (
            client.post(url + "/confirm", data={"revision": 1}, follow_redirects=False).status_code
            == 303
        )
    assert len(session.scalars(select(Parcel)).all()) == 1


def test_source_references_are_scoped_to_the_file_and_farm(client, session, settings):
    import json

    farm = setup_farm(session, settings)
    first = upload(client)
    assert (
        client.post(
            first.headers["location"] + "/confirm", data={"revision": 1}, follow_redirects=False
        ).status_code
        == 303
    )
    doc = json.loads((FIXTURES / "parcel.geojson").read_bytes())
    for point in doc["features"][0]["geometry"]["coordinates"][0]:
        point[0] += 0.01
    # Stable reference in the same source means an existing parcel, even with a changed boundary.
    assert upload(client, "parcel.geojson", json.dumps(doc).encode()).status_code == 200
    # Local reference 'synthetic-1' in another source is not the same parcel.
    new = upload(client, "different.geojson", json.dumps(doc).encode())
    assert new.status_code == 303
    other = Farm(name="Separate importing farm", props={"country": "FR"})
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    response = client.post(
        f"/onboarding/import?farm={other.id}",
        files={"file": ("parcel.geojson", (FIXTURES / "parcel.geojson").read_bytes())},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert session.scalar(select(Parcel).where(Parcel.farm_id == farm.id)) is not None
    assert not session.scalars(select(Parcel).where(Parcel.farm_id == other.id)).all()


def test_anonymous_upload_cannot_propose_parcels(client, session, settings):
    setup_farm(session, settings)
    del client.headers["X-Admin-Token"]
    response = upload(client)
    assert response.status_code in {401, 403}
    assert not session.scalars(select(Draft)).all()


def test_chunked_request_is_capped_before_multipart_parsing(client, session, settings):
    setup_farm(session, settings)
    response = client.post(
        "/onboarding/import",
        content=(b"x" * 100_000 for _ in range(51)),
        headers={"Content-Type": "multipart/form-data; boundary=synthetic"},
    )
    assert response.status_code == 413
    assert not session.scalars(select(Draft)).all()
