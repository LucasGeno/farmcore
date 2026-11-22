"""Global FieldID lookups (Varda): an optional, read-only lookup that keeps a parcel's GFID as an
`ext_id` alias. No live credentials exist, so the fixtures follow the documented API shapes
(`fixtures/varda/README.md`), not recorded responses. Only Varda's HTTP is replaced."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.fields import field_id
from farmcore.apps.fields.manifest import MANIFEST as FIELDS
from farmcore.core import clock
from farmcore.core.access import FARM_COOKIE, ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import ExtId, Farm
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from farmcore.settings import DEFAULT_VARDA_API_BASE_URL, DEFAULT_VARDA_TOKEN_URL
from tests.apps.operations.conftest import make_parcel

FIXTURES = Path(__file__).parent / "fixtures" / "varda"
MATCH = field_id.Candidate("15RJ.CZM7", "0193a3c1-6f2e-7c4a-9d1b-2f5e8a7c4b10", 0.93, 0.97, 0.95)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeVarda:
    """Varda's token and field-search endpoints; `search` names the search fixture."""

    def __init__(self, search: str = "search_match") -> None:
        self.search, self.calls, self.tokens, self.refuse_next = search, [], 0, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/oauth/token":
            self.tokens += 1
            return httpx.Response(
                200, json={**_fixture("token"), "access_token": f"token-{self.tokens}"}
            )
        if self.refuse_next:
            self.refuse_next = False
            body = {"error": {"title": "Unauthorized", "status": 401, "detail": "expired"}}
            return httpx.Response(401, json=body)
        return httpx.Response(200, json=_fixture(self.search))

    def searches(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.path.endswith("/field-searches")]


def client_for(fake: FakeVarda, clock=None) -> field_id.VardaClient:
    return field_id.VardaClient(
        "client-under-test",
        "secret-under-test",
        api_url=DEFAULT_VARDA_API_BASE_URL,
        token_url=DEFAULT_VARDA_TOKEN_URL,
        transport=httpx.MockTransport(fake),
        **({"clock": clock} if clock else {}),
    )


def _repo(session, farm: Farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")


@pytest.fixture
def farms(session, settings):
    farm = ensure_default_farm(session, settings)
    other = Farm(name="Second farm in this installation")
    session.add(other)
    session.flush()
    parcel = make_parcel(session, farm, 1, area="1.25", props={})
    foreign = make_parcel(session, other, 2, area="0.8", props={})
    session.commit()
    return farm, other, parcel, foreign


def _kept(session, parcel) -> set[tuple[str, str]]:
    rows = session.scalars(
        select(ExtId).where(
            ExtId.model == "parcel",
            ExtId.res_id == parcel.id,
            ExtId.source.in_((field_id.GFID_SOURCE, field_id.BOUNDARY_SOURCE)),
        )
    )
    return {(row.source, row.key) for row in rows}


def test_the_token_is_reused_until_it_expires_and_renewed_once_when_refused():
    fake, now = FakeVarda(), [1000.0]
    client = client_for(fake, clock=lambda: now[0])
    square = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]}

    client.search(square)
    client.search(square)
    assert fake.tokens == 1
    now[0] += 3600  # the fixture's expires_in
    client.search(square)
    assert fake.tokens == 2
    fake.refuse_next = True  # refused before its expiry: renewed once, the search retried
    assert [c.field_id for c in client.search(square)] == ["15RJ.CZM7", "7KQW.R3TN"]
    assert fake.tokens == 3

    token = next(c for c in fake.calls if c.url.path == "/oauth/token")
    assert parse_qs(token.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["client-under-test"],
        "client_secret": ["secret-under-test"],
        "audience": ["https://api.varda.ag/fid/"],
    }
    search = fake.searches()[-1]
    assert search.headers["Authorization"] == "Bearer token-3"
    assert search.headers["Content-Type"] == "application/geo+json"
    assert json.loads(search.content) == square
    assert all(b"secret-under-test" not in c.content for c in fake.searches())


@pytest.mark.parametrize(
    ("fixture", "kept", "candidates"),
    [
        ("search_match", "15RJ.CZM7", 2),
        ("search_partial", None, 2),
        ("search_none", None, 0),
    ],
)
def test_only_a_same_field_match_is_kept_otherwise_candidates_are_shown(
    session, farms, fixture, kept, candidates
):
    farm, _, parcel, _ = farms
    repo, fake = _repo(session, farm), FakeVarda(fixture)

    result = field_id.look_up(repo, parcel, client_for(fake))

    assert (result.kept.field_id if result.kept else None) == kept
    assert len(result.candidates) == candidates
    assert json.loads(fake.searches()[0].content)["type"] == "MultiPolygon"  # the parcel's shape
    assert field_id.alias_of(repo, parcel) == kept
    if kept:
        assert _kept(session, parcel) == {
            (field_id.GFID_SOURCE, "15RJ.CZM7"),
            (field_id.BOUNDARY_SOURCE, "0193a3c1-6f2e-7c4a-9d1b-2f5e8a7c4b10"),
        }
    else:
        assert _kept(session, parcel) == set()
    if fixture == "search_partial":  # the overlap figures reach the owner as Varda sent them
        assert result.candidates[0] == field_id.Candidate(
            "15RJ.CZM7", "0193a3c1-6f2e-7c4a-9d1b-2f5e8a7c4b10", 0.42, 0.45, 0.88
        )


def test_a_field_id_kept_for_another_farm_is_refused(session, farms):
    farm, other, parcel, foreign = farms
    field_id.keep(_repo(session, farm), parcel, MATCH)
    session.commit()

    with pytest.raises(field_id.AlreadyClaimed, match="already claimed in this installation"):
        field_id.keep(_repo(session, other), foreign, MATCH)

    assert field_id.alias_of(_repo(session, farm), parcel) == "15RJ.CZM7"
    assert _kept(session, foreign) == set()


@pytest.fixture
def web(settings, session_factory, session, farms):
    app = create_app(settings, manifests=[FIELDS], session_factory=session_factory)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        c.cookies.set(FARM_COOKIE, str(farms[0].id))
        yield c


def test_without_credentials_or_in_replay_nothing_calls_out(web, farms, monkeypatch):
    parcel = farms[2]

    def no_network(*args, **kwargs):
        raise AssertionError("no outbound call without credentials")

    monkeypatch.setattr(httpx, "Client", no_network)
    page = web.get(f"/fields/parcels/{parcel.id}")
    assert page.status_code == 200 and "Look up Global FieldID" not in page.text
    assert web.post(f"/fields/parcels/{parcel.id}/field-id/lookup").status_code == 404

    fake = FakeVarda()  # credentials, but the clock is a replay
    web.app.dependency_overrides[field_id.get_client] = lambda: client_for(fake)
    page = web.get(f"/fields/parcels/{parcel.id}")
    assert "Look up Global FieldID" not in page.text
    assert web.post(f"/fields/parcels/{parcel.id}/field-id/lookup").status_code == 409
    assert fake.calls == []


def test_an_owner_chooses_among_candidates_and_the_parcel_shows_its_field_id(
    web, session, settings, farms
):
    parcel = farms[2]
    clock.set_live(session, settings)
    session.commit()
    web.app.dependency_overrides[field_id.get_client] = lambda: client_for(
        FakeVarda("search_partial")
    )
    assert "Look up Global FieldID" in web.get(f"/fields/parcels/{parcel.id}").text

    found = web.post(f"/fields/parcels/{parcel.id}/field-id/lookup")
    assert found.status_code == 200
    assert "IoU 0.42" in found.text and "7KQW.R3TN" in found.text
    assert "Global FieldID 15RJ.CZM7" not in web.get(f"/fields/parcels/{parcel.id}").text

    url = f"/fields/parcels/{parcel.id}/field-id"
    assert web.post(url, data={"field_id": "ZZZZ.ZZZZ"}).status_code == 422  # not a candidate
    kept = web.post(url, data={"field_id": "15RJ.CZM7"}, follow_redirects=False)
    assert kept.status_code == 303
    assert "Global FieldID 15RJ.CZM7" in web.get(f"/fields/parcels/{parcel.id}").text
