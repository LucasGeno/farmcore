"""Propose tools: an agent proposes parcels, crops and records as drafts only.

Failure model (one owning test each):
- a propose tool writes an open draft, never a confirmed record (no log, parcel or crop season),
  and no confirm/cancel/delete tool exists on the surface;
- `get_draft` reads the agent's own draft and refuses a cross-farm id (not found);
- `propose_parcels` rejects invalid or out-of-range geometry (point, non-WGS84, tiny area, over
  the vertex cap);
- `propose_record` rejects a parcel that is not on the token's farm;
- a token without `drafts:propose` is not offered a propose tool and is refused at call;
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
POINT = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Point", "coordinates": [-0.15, 44.89]},
        }
    ],
}
NON_WGS84 = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[200, 89], [200.01, 89], [200.01, 89.01], [200, 89]]],
            },
        }
    ],
}
TINY = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-0.15, 44.89],
                        [-0.15 + 1e-6, 44.89],
                        [-0.15 + 1e-6, 44.89 + 1e-6],
                        [-0.15, 44.89],
                    ]
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
        ("spray", {"doses": [{"product_id": product.id, "value": "2", "unit": "kg/ha"}]}),
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
    parcels = tools.call(raw, "propose_parcels", {"geojson": GEOJSON})

    assert record.facts["type"] == "observation"
    assert parcels.facts["parcels"] == 1
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
    names = {t.name for t in tools.list(raw)}
    assert not any(word in n for n in names for word in ("confirm", "cancel", "revise", "delete"))
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
    [POINT, NON_WGS84, TINY, VERTEX_CAP],
    ids=["point", "crs", "tiny", "vertex"],
)
def test_propose_parcels_rejects_invalid_or_out_of_range_geometry(
    session, session_factory, settings, manifests, geojson
):
    _, _, raw, _ = _farm(session, ["drafts:propose", "fields:read"])
    tools = _tools(session_factory, settings, manifests)

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


def test_a_token_without_drafts_propose_is_not_offered_and_is_refused(
    session, session_factory, settings, manifests
):
    _, _, raw, _ = _farm(session, ["fields:read", "logs:read"])
    tools = _tools(session_factory, settings, manifests)

    names = {t.name for t in tools.list(raw)}
    proposal = {"propose_record", "propose_parcels", "propose_crop_assignments", "get_draft"}
    assert not names & proposal

    with pytest.raises(farm_mcp.Refused) as refused:
        tools.call(raw, "propose_record", {})
    assert "not available" in str(refused.value).lower()


@pytest.mark.parametrize("empty", [False, True])
def test_list_import_candidates_returns_validated_candidates(
    session, session_factory, settings, manifests, monkeypatch, empty
):
    import httpx

    _, _, raw, _ = _farm(session, ["drafts:propose", "fields:read"])
    tools = _tools(session_factory, settings, manifests)
    feature = GEOJSON["features"][0] | {"properties": {"id_parcel": "1", "code_cultu": "VRC"}}
    real = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"type": "FeatureCollection", "features": [] if empty else [feature]}
        )
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    result = tools.call(raw, "list_import_candidates", {"bbox": "-0.159,44.881,-0.141,44.899"})
    if empty:
        assert result.facts["candidates"] == []
    else:
        [candidate] = result.facts["candidates"]
        assert candidate["source_ref"] == "1"
        assert candidate["crop_key"] == "vine"


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
            | {"features": [GEOJSON["features"][0] | {"properties": {"id_parcel": "1"}}]},
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )
    args = {"bbox": "-0.159,44.881,-0.141,44.899"}
    for _ in range(6):
        _tools(session_factory, settings, manifests).call(raw, "list_import_candidates", args)
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


def test_crop_proposal_review_shows_proposed_choices(
    client, session, session_factory, settings, manifests
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
    confirmed = client.post(f"/drafts/{draft_id}/confirm?farm={farm.id}", data={"revision": 1})
    assert confirmed.status_code == 200
    [season] = session.scalars(select(CropSeason)).all()
    assert season.parcel_id == parcel_id and season.crop_name == "Olive"


def test_propose_tool_respects_kind_payload_budget(session, session_factory, settings, manifests):
    from dataclasses import replace

    _, _, raw, _ = _farm(session, ["drafts:propose"])
    bounded = [
        replace(
            m,
            draft_kinds=tuple(
                replace(k, max_payload_bytes=100) if k.key == "parcels" else k
                for k in m.draft_kinds
            ),
        )
        for m in manifests
    ]
    with pytest.raises(farm_mcp.Refused, match="too large"):
        _tools(session_factory, settings, bounded).call(
            raw, "propose_parcels", {"geojson": GEOJSON}
        )
    assert session.scalars(select(Draft)).all() == []
    assert session.scalar(select(AccessLog.resource)) == "mcp:propose_parcels"
