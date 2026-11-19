"""Public CAP claims: recorded provider HTTP, then the normal parcel draft seam."""

import json
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from farmcore.core.models.canonical import CropSeason, ExtId, Parcel, Source
from farmcore.core.models.drafts import Draft
from farmcore.core.seed import ensure_default_farm

FIXTURES = Path(__file__).parent / "fixtures" / "registries"


@pytest.mark.parametrize(
    "country,lon,lat,reference,code,licence",
    [
        ("FR", -0.2895, 48.9605, "1660019", "PPH", "Etalab 2.0"),
        ("ES", -4.7995, 41.7005, "47:67:0:0:23:443:5", "1", "CC BY 4.0"),
    ],
)
def test_registry_selection_reviews_then_confirms_with_provenance(
    client, session, settings, monkeypatch, country, lon, lat, reference, code, licence
):
    farm = ensure_default_farm(session, settings)
    farm.props = {
        **farm.props,
        "country": country,
        "forecast_location": {"longitude": lon, "latitude": lat},
    }
    session.commit()
    requests = []

    def answer(request):
        requests.append(request)
        name = "rpg" if request.url.host == "apicarto.ign.fr" else request.url.path.split("/")[-2]
        return httpx.Response(200, json=json.loads((FIXTURES / (name + ".json")).read_text()))

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    bbox = f"{lon - 0.0005},{lat - 0.0005},{lon + 0.0005},{lat + 0.0005}"
    page = client.get("/onboarding/registry")
    assert page.status_code == 200
    found = client.get("/onboarding/registry/features", params={"bbox": bbox})
    assert found.status_code == 200, found.text
    row = next(f for f in found.json()["features"] if f["properties"]["source_ref"] == reference)
    assert row["properties"]["crop_code"] == code
    assert row["properties"]["licence"] == licence
    assert not session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()
    signed = found.json()["candidates"]
    calls_before_claim = len(requests)
    # A signed snapshot is data, not authority; altered and expired maps cannot propose.
    tampered = ("0" if signed[0] != "0" else "1") + signed[1:]
    rejected = client.post(
        "/onboarding/registry/claim", data={"candidates": tampered, "selected": reference}
    )
    assert rejected.status_code == 200 and "Reload the public map" in rejected.text
    assert not session.scalars(select(Draft).where(Draft.farm_id == farm.id)).all()
    from farmcore.apps.onboarding.registries import candidates as snapshots

    with monkeypatch.context() as clock:
        clock.setattr(snapshots.time, "time", lambda: 99999999999)
        expired = client.post(
            "/onboarding/registry/claim", data={"candidates": signed, "selected": reference}
        )
    assert expired.status_code == 200 and "Reload the public map" in expired.text
    assert not session.scalars(select(Draft).where(Draft.farm_id == farm.id)).all()
    claimed = client.post(
        "/onboarding/registry/claim",
        data={"candidates": signed, "selected": reference},
        follow_redirects=False,
    )
    assert claimed.status_code == 303, claimed.text
    assert len(requests) == calls_before_claim
    draft = session.scalar(select(Draft).where(Draft.farm_id == farm.id))
    assert draft.kind == "parcels"
    assert draft.provenance["source_type"] == "registry"
    assert len(draft.payload["parcels"]) == 1
    item = draft.payload["parcels"][0]
    assert item["source_ref"] == reference and item["crop_code"] == code
    assert item["licence"] == licence and item["attribution"]
    assert item["year"] == (2024 if country == "FR" else None)
    assert item["crop_key"] == ("grassland" if country == "FR" else "soft-wheat")
    assert item["crop_name"] == ("Grassland" if country == "FR" else "Soft wheat")
    review = client.get(f"/onboarding/import/review?draft={draft.id}")
    assert licence in review.text
    if country == "ES":
        timestamp = json.loads((FIXTURES / "recintos.json").read_text())["timeStamp"]
        assert item["retrieved_at"] == timestamp
        assert timestamp in review.text
    assert (
        client.post(
            f"/drafts/{draft.id}/confirm", data={"revision": 1}, follow_redirects=False
        ).status_code
        == 303
    )
    session.expire_all()
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id))
    # Registry claims use the same owner-reviewed, dated season assignment as file imports.
    assert not session.scalars(select(CropSeason).where(CropSeason.parcel_id == parcel.id)).all()
    crop_page = client.get("/onboarding/crops")
    crop_row = re.search(rf'<tr data-parcel="{parcel.id}".*?</tr>', crop_page.text, re.S)
    assert crop_row is not None
    if country == "FR":
        assert 'data-crop="grassland"' in crop_row.group()
        assigned = client.post(
            "/onboarding/crops/assign",
            data={
                "starts_on": "2024-03-01",
                "parcel": str(parcel.id),
                f"crop_{parcel.id}": "grassland",
            },
            follow_redirects=False,
        )
        assert assigned.status_code == 303
        season = session.scalar(select(CropSeason).where(CropSeason.parcel_id == parcel.id))
        assert (season.crop_name, season.crop_eppo) == ("Grassland", None)
    else:
        assert 'data-crop="soft-wheat"' in crop_row.group()
        assert 'data-unknown-code=""' in crop_row.group()
        assert '<code class="font-mono text-[14px]">1</code>' in crop_row.group()
        assert not session.scalars(
            select(CropSeason).where(CropSeason.parcel_id == parcel.id)
        ).all()
    assert parcel.iacs_id == reference
    source_scope = "ign-rpg-2024" if country == "FR" else "sigpac-current"
    ext = session.scalar(select(ExtId).where(ExtId.res_id == parcel.id, ExtId.model == "parcel"))
    assert (ext.source, ext.key) == (source_scope, f"farm:{farm.id}:{reference}")
    source = session.scalar(select(Source).where(Source.code == source_scope))
    assert (source.name, source.kind, source.licence) == (item["attribution"], "geodata", licence)
    assert parcel.props["registry"]["licence"] == licence
    assert parcel.props["import_source_ref"] == reference
    again = client.post(
        "/onboarding/registry/claim", data={"candidates": signed, "selected": reference}
    )
    assert again.status_code == 200 and "already imported" in again.text
    assert len(session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()) == 1
    for request in requests:
        assert request.method == "GET" and not request.content
        assert request.headers["accept-encoding"] == "identity"
        assert request.url.host in {"apicarto.ign.fr", "sigpac-hubcloud.es"}
        assert set(request.url.params) <= {"geom", "annee", "bbox", "limit", "f"}
        if country == "FR":
            assert json.loads(request.url.params["geom"])["type"] == "Polygon"
        else:
            assert request.url.params["bbox"] == bbox
    assert {r.url.path.split("/")[-2] for r in requests} >= (
        {"recintos", "cultivo_declarado"} if country == "ES" else {"rpg"}
    )


@pytest.mark.parametrize(
    "failure", ["redirect", "oversize", "deadline", "malformed", "distant", "bad_geometry"]
)
def test_registry_failure_offers_file_import_without_a_draft(
    client, session, settings, monkeypatch, failure
):
    import asyncio

    from farmcore.apps.onboarding import registries

    farm = ensure_default_farm(session, settings)
    farm.props = {
        **farm.props,
        "country": "FR",
        "forecast_location": {"longitude": -0.2895, "latitude": 48.9605},
    }
    session.commit()
    requests = []

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

    def answer(request):
        requests.append(request)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://attacker.invalid/private"})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * (registries.MAX_BYTES + 1))
        if failure == "deadline":
            return httpx.Response(200, stream=StalledBody())
        if failure == "bad_geometry":
            data = json.loads((FIXTURES / "rpg.json").read_text())
            data["features"][0]["geometry"] = {"type": "Foo"}
            return httpx.Response(200, json=data)
        return httpx.Response(200, json={"features": "bad"})

    if failure == "deadline":
        monkeypatch.setattr(registries, "DEADLINE_SECONDS", 0.01)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    bbox = "100,40,100.001,40.001" if failure == "distant" else "-0.290,48.960,-0.289,48.961"
    response = client.get("/onboarding/registry/features", params={"bbox": bbox})
    assert response.status_code == 200
    assert response.json()["features"] == []
    assert "import a file instead" in response.json()["message"].lower()
    claim = client.post("/onboarding/registry/claim", data={"bbox": bbox, "selected": "1660019"})
    assert claim.status_code == 200
    assert "import a file instead" in claim.text.lower()
    assert not session.scalars(select(Draft).where(Draft.farm_id == farm.id)).all()
    assert len(requests) == (0 if failure == "distant" else 1)
    assert all(r.url.host == "apicarto.ign.fr" for r in requests)


def test_registry_does_not_send_queries_for_unauthenticated_visitors(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Anonymous request must not contact a registry")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    client.headers.pop("X-Admin-Token")
    for path in ("/onboarding/registry", "/onboarding/registry/features?bbox=-1,48,0,49"):
        assert client.get(path, follow_redirects=False).status_code in (303, 401, 403)
    assert client.post(
        "/onboarding/registry/claim", data={"selected": "1660019"}, follow_redirects=False
    ).status_code in (303, 401, 403)
