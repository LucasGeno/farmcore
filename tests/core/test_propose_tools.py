"""Propose tools: an agent proposes parcels, crops and records as drafts only.

Failure model (one owning test each):
- a propose tool writes an open draft, never a confirmed record (no log, parcel or crop season),
  and no confirm/cancel/delete tool exists on the surface;
- `get_draft` reads the agent's own draft and refuses a cross-farm id (not found);
- `propose_parcels` rejects invalid geometry or oversized input
  (CRS override, vertex cap and byte budgets);
- `propose_record` rejects a parcel that is not on the token's farm;
- scope listing/call refusals are owned by test_mcp_stdio;
- `list_import_candidates` returns validated candidates (the public lookup is faked).
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import select

from farmcore.apps.onboarding.importers import parsers
from farmcore.core import mcp as farm_mcp
from farmcore.core import registry
from farmcore.core.models.canonical import AccessLog, CropSeason, Log, Parcel, Product
from farmcore.core.models.drafts import Draft
from tests.core.test_mcp_stdio import _token

GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "New field"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-0.14, 44.89], [-0.13, 44.89], [-0.13, 44.90], [-0.14, 44.90], [-0.14, 44.89]]
                ],
            },
        }
    ],
}
VERTEX_CAP = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [
                            -0.15 + 0.001 * math.cos(2 * math.pi * i / (parsers.MAX_VERTICES + 1)),
                            44.89 + 0.001 * math.sin(2 * math.pi * i / (parsers.MAX_VERTICES + 1)),
                        ]
                        for i in range(parsers.MAX_VERTICES + 1)
                    ]
                ],
            },
        }
    ],
}


def _farm(session, scopes, *, name="MCP farm"):
    """`_token`, plus a country and a location so parcel and candidate validation can run."""
    farm, token_id, raw = _token(session, scopes, farm_name=name)
    farm.props = {
        **(farm.props or {}),
        "country": "FR",
        "forecast_location": {"latitude": 44.89, "longitude": -0.15},
    }
    session.flush()
    parcel_id = session.scalar(select(Parcel.id).where(Parcel.farm_id == farm.id))
    return farm, token_id, raw, parcel_id


@pytest.fixture(scope="module")
def manifests():
    return registry.discover()


def _tools(session_factory, settings, manifests):
    return farm_mcp.FarmTools(session_factory, settings, manifests)


def test_propose_tools_write_open_drafts_and_nothing_else(
    session, session_factory, settings, manifests
):
    farm, token_id, raw, parcel_id = _farm(session, ["drafts:propose", "fields:read", "logs:read"])
    tools = _tools(session_factory, settings, manifests)

    record = tools.call(
        raw,
        "propose_record",
        {
            "type": "observation",
            "parcel_ids": [parcel_id],
            "ts_start": "2024-05-01T10:00:00+02:00",
            "fact": {"text": "aphids on the leaves"},
        },
    )
    product = Product(
        name="Test copper",
        authorisation_no="test",
        active_substance="copper",
        product_type="contact",
    )
    session.add(product)
    session.flush()
    for kind, fields in (
        ("spray", {"doses": [{"catalog_item_id": product.id, "value": "2", "unit": "kg/ha"}]}),
        ("irrigation", {"fact": {"amount": "10", "unit": "mm"}}),
    ):
        tools.call(
            raw,
            "propose_record",
            {
                "type": kind,
                "parcel_ids": [parcel_id],
                "ts_start": "2024-05-01T10:00:00+02:00",
                **fields,
            },
        )
    assignments = tools.call(
        raw,
        "propose_crop_assignments",
        {"starts_on": "2024-05-01", "assignments": {str(parcel_id): "vine"}},
    )
    parcels = tools.call(
        raw,
        "propose_parcels",
        {
            "geojson": GEOJSON
            | {
                "features": [
                    GEOJSON["features"][0]
                    | {"properties": {"name": "New field", "source_ref": "spoof"}}
                ]
            }
        },
    )

    assert record.facts["type"] == "observation"
    assert parcels.facts["parcels"] == 1
    assert session.get(Draft, parcels.facts["draft_id"]).payload["parcels"][0]["source_ref"] == ""
    assert assignments.facts["parcels"] == 1
    rows = session.scalars(select(Draft)).all()
    assert {d.kind for d in rows} == {
        "spray",
        "irrigation",
        "observation",
        "crop-assignments",
        "parcels",
    }
    assert all(d.status == "open" and d.actor == f"token:{token_id}" for d in rows)
    assert session.scalars(select(Log)).all() == []  # no record was written
    assert session.scalars(select(CropSeason)).all() == []  # no season was written
    assert len(session.scalars(select(Parcel)).all()) == 1  # the original parcel only
    # No confirm, cancel, revise or delete tool is on the surface.
    assert all(t.effect in {"read", "propose"} for t in tools.list(raw))
    # Every call was audited as the token, one row per tool.
    audited = session.execute(
        select(AccessLog.actor, AccessLog.resource).where(AccessLog.farm_id == farm.id)
    ).all()
    assert {actor for actor, _ in audited} == {f"token:{token_id}"}
    assert {resource for _, resource in audited} == {
        "mcp:propose_record",
        "mcp:propose_crop_assignments",
        "mcp:propose_parcels",
    }


def test_get_draft_reads_its_own_and_refuses_a_cross_farm_id(
    session, session_factory, settings, manifests
):
    farm, token_id, raw, parcel_id = _farm(session, ["drafts:propose", "fields:read", "logs:read"])
    other, _, other_raw, _ = _farm(session, ["drafts:propose"], name="Other farm")
    tools = _tools(session_factory, settings, manifests)
    draft_id = tools.call(
        raw,
        "propose_record",
        {
            "type": "observation",
            "parcel_ids": [parcel_id],
            "ts_start": "2024-05-01T10:00:00+02:00",
            "fact": {"text": "aphids on the leaves"},
        },
    ).facts["draft_id"]

    own = tools.call(raw, "get_draft", {"draft_id": draft_id})
    assert own.facts["draft_id"] == draft_id
    assert own.facts["kind"] == "observation"

    with pytest.raises(farm_mcp.Refused) as refused:
        tools.call(other_raw, "get_draft", {"draft_id": draft_id})
    assert "not found" in str(refused.value).lower()


@pytest.mark.parametrize(
    "geojson",
    [
        GEOJSON | {"crs": {"type": "name", "properties": {"name": "EPSG:2154"}}},
        VERTEX_CAP,
        GEOJSON | {"padding": "x" * 2_000_001},
        None,
    ],
    ids=["crs", "vertex", "input-size", "kind-budget"],
)
def test_propose_parcels_rejects_invalid_or_out_of_range_geometry(
    session, session_factory, settings, manifests, geojson
):
    _, _, raw, _ = _farm(session, ["drafts:propose", "fields:read"])
    tools = _tools(session_factory, settings, manifests)

    if geojson is None:
        from dataclasses import replace

        manifests = [
            replace(
                m,
                draft_kinds=tuple(
                    replace(k, max_payload_bytes=100) if k.key == "parcels" else k
                    for k in m.draft_kinds
                ),
            )
            for m in manifests
        ]
        tools = _tools(session_factory, settings, manifests)
        geojson = GEOJSON
    with pytest.raises(farm_mcp.Refused):
        tools.call(raw, "propose_parcels", {"geojson": geojson})
    assert session.scalars(select(Draft)).all() == []  # nothing was proposed


def test_propose_record_rejects_a_parcel_of_another_farm(
    session, session_factory, settings, manifests
):
    farm, _id, raw, _ = _farm(session, ["drafts:propose", "fields:read", "logs:read"])
    other, _, _, other_parcel = _farm(session, ["drafts:propose"], name="Other farm")
    tools = _tools(session_factory, settings, manifests)

    with pytest.raises(farm_mcp.Refused) as refused:
        tools.call(
            raw,
            "propose_record",
            {
                "type": "observation",
                "parcel_ids": [other_parcel],
                "ts_start": "2024-05-01T10:00:00+02:00",
                "fact": {"text": "aphids"},
            },
        )
    assert "not on this farm" in str(refused.value).lower()
    assert session.scalars(select(Draft)).all() == []


def test_registry_budget_survives_new_service_instances(
    session, session_factory, settings, manifests, monkeypatch
):
    from datetime import timedelta

    import httpx

    _, _, raw, _ = _farm(session, ["fields:read"])
    _, _, other, _ = _farm(session, ["fields:read"], name="Other")
    requests = []

    def answer(request):
        requests.append(request)
        assert request.url.host == "apicarto.ign.fr"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            json=GEOJSON
            | {
                "features": []
                if len(requests) == 2
                else [
                    GEOJSON["features"][0] | {"properties": {"id_parcel": "1", "code_cultu": "VRC"}}
                ]
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    args = {"bbox": "-0.159,44.881,-0.141,44.899"}
    for index in range(6):
        result = _tools(session_factory, settings, manifests).call(
            raw, "list_import_candidates", args
        )
        if index == 1:
            assert result.facts["candidates"] == []
        else:
            [candidate] = result.facts["candidates"]
            assert candidate["source_ref"] == "1" and candidate["crop_key"] == "vine"
    with pytest.raises(farm_mcp.Refused, match="rate limit"):
        _tools(session_factory, settings, manifests).call(raw, "list_import_candidates", args)
    assert len(requests) == 6
    _tools(session_factory, settings, manifests).call(other, "list_import_candidates", args)
    assert len(requests) == 7
    for row in session.scalars(select(AccessLog)):
        row.ts -= timedelta(minutes=2)
    session.flush()
    _tools(session_factory, settings, manifests).call(raw, "list_import_candidates", args)
    assert len(requests) == 8
    assert session.scalars(select(Draft)).all() == []


@pytest.mark.parametrize("scenario", ["new", "future", "deleted", "unknown"])
def test_crop_proposal_review_shows_proposed_choices(
    client, session, session_factory, settings, manifests, scenario
):
    farm, _, raw, parcel_id = _farm(session, ["drafts:propose"])
    result = _tools(session_factory, settings, manifests).call(
        raw,
        "propose_crop_assignments",
        {"starts_on": "2024-05-01", "assignments": {str(parcel_id): "olive"}},
    )
    draft_id = result.facts["draft_id"]
    from tests.core.test_member_sessions import _login

    client.base_url = "https://testserver"
    client.headers.pop("X-Admin-Token", None)
    assert _login(client, login=f"mcp-owner-{farm.id}", password="x" * 16).status_code == 303
    review = client.get(f"/onboarding/crops/review?farm={farm.id}&draft={draft_id}")
    assert review.status_code == 200
    assert "2024-05-01" in review.text and "Olive" in review.text and "Bel-Air" in review.text
    assert session.scalars(select(CropSeason)).all() == []

    client.headers.update(
        {"Origin": "https://testserver", "X-CSRF-Token": client.cookies["farmcore_csrf"]}
    )
    if scenario == "future":
        from datetime import date

        session.add(
            CropSeason(
                farm_id=farm.id,
                parcel_id=parcel_id,
                starts_on=date(2024, 6, 1),
                crop_name="Vine",
                created_by="operator",
            )
        )
        session.flush()
    if scenario == "future":
        warning = client.get(f"/onboarding/crops/review?farm={farm.id}&draft={draft_id}")
        assert warning.status_code == 200 and "Overlaps a future crop season" in warning.text
    if scenario in {"deleted", "unknown"}:
        if scenario == "deleted":
            session.delete(session.get(Parcel, parcel_id))
        else:
            row = session.get(Draft, draft_id)
            row.payload = {**row.payload, "assignments": {str(parcel_id): "retired-crop"}}
        session.flush()
        stale = client.get(f"/onboarding/crops/review?farm={farm.id}&draft={draft_id}")
        assert stale.status_code == 409 and "no longer valid" in stale.text
        return
    confirmed = client.post(f"/drafts/{draft_id}/confirm?farm={farm.id}", data={"revision": 1})
    if scenario == "future":
        assert confirmed.status_code == 422
        session.expire_all()
        assert session.get(Draft, draft_id).status == "open"
        [season] = session.scalars(select(CropSeason)).all()
        assert season.crop_name == "Vine"
        return
    assert confirmed.status_code == 200
    [season] = session.scalars(select(CropSeason)).all()
    assert season.parcel_id == parcel_id and season.crop_name == "Olive"


def test_crop_proposal_rejects_duplicate_numeric_parcel_ids(
    session, session_factory, settings, manifests
):
    _, _, raw, parcel_id = _farm(session, ["drafts:propose"])
    with pytest.raises(farm_mcp.Refused, match="once"):
        _tools(session_factory, settings, manifests).call(
            raw,
            "propose_crop_assignments",
            {
                "starts_on": "2024-05-01",
                "assignments": {str(parcel_id): "vine", f"0{parcel_id}": "olive"},
            },
        )
    for assignments, reason in [
        ({"²": "olive"}, "Parcel ids"),
        ({str(parcel_id): 1}, "assignments"),
    ]:
        with pytest.raises(farm_mcp.Refused) as refused:
            _tools(session_factory, settings, manifests).call(
                raw,
                "propose_crop_assignments",
                {"starts_on": "2024-05-01", "assignments": assignments},
            )
        assert reason in str(refused.value) or str(refused.value) == farm_mcp.BAD_ARGUMENTS
        assert "pydantic" not in str(refused.value) and "invalid literal" not in str(refused.value)
    with pytest.raises(farm_mcp.Refused, match="fact.log_id: unexpected field"):
        _tools(session_factory, settings, manifests).call(
            raw,
            "propose_record",
            {
                "type": "observation",
                "parcel_ids": [parcel_id],
                "ts_start": "2024-05-01T10:00:00Z",
                "fact": {"text": "aphids", "log_id": 1},
            },
        )
    assert session.scalars(select(Draft)).all() == []


def test_crop_confirm_records_only_uncovered_parcels(
    client, session, session_factory, settings, manifests
):
    from datetime import date

    from tests.core.test_member_sessions import _login

    farm, _, raw, covered_id = _farm(session, ["drafts:propose"])
    covered = CropSeason(
        farm_id=farm.id,
        parcel_id=covered_id,
        starts_on=date(2023, 1, 1),
        crop_name="Vine",
        created_by="operator",
    )
    parcel = Parcel(
        farm_id=farm.id,
        name="New parcel",
        alias="P2",
        area_ha=1,
        geom=session.get(Parcel, covered_id).geom,
    )
    session.add_all([covered, parcel])
    session.flush()
    client.base_url = "https://testserver"
    client.headers.pop("X-Admin-Token", None)
    assert _login(client, login=f"mcp-owner-{farm.id}", password="x" * 16).status_code == 303
    client.headers.update(
        {"Origin": "https://testserver", "X-CSRF-Token": client.cookies["farmcore_csrf"]}
    )
    tools = _tools(session_factory, settings, manifests)
    for choices, status in [
        ({str(covered_id): "olive"}, 422),
        ({str(covered_id): "olive", str(parcel.id): "olive"}, 200),
    ]:
        draft_id = tools.call(
            raw, "propose_crop_assignments", {"starts_on": "2024-05-01", "assignments": choices}
        ).facts["draft_id"]
        review = client.get(f"/onboarding/crops/review?farm={farm.id}&draft={draft_id}")
        assert "Covered: keeps Vine" in review.text
        response = client.post(f"/drafts/{draft_id}/confirm?farm={farm.id}", data={"revision": 1})
        assert response.status_code == status
        session.expire_all()
        draft = session.get(Draft, draft_id)
        if status == 422:
            assert "nothing would be recorded" in response.text.lower()
            assert draft.status == "open" and draft.result_id is None
        else:
            assert draft.result_id != covered.id
            created = session.get(CropSeason, draft.result_id)
            assert created.parcel_id == parcel.id and created.crop_name == "Olive"
        assert session.get(CropSeason, covered.id).crop_name == "Vine"
