"""Global FieldID lookups (Varda): an optional, read-only lookup that keeps a parcel's GFID as an
`ext_id` alias. No live credentials exist, so the fixtures follow the documented API shapes
(`fixtures/varda/README.md`), not recorded responses. Only Varda's HTTP is replaced."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from farmcore.apps.fields import field_id
from farmcore.apps.fields.manifest import MANIFEST as FIELDS
from farmcore.apps.share import service as share_service
from farmcore.core import clock
from farmcore.core.access import FARM_COOKIE, ScopedRepository, ensure_owner_grant
from farmcore.core.identity import DUMMY_HASH, token_hash
from farmcore.core.models.canonical import ExtId, Farm
from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from farmcore.settings import DEFAULT_VARDA_API_BASE_URL, DEFAULT_VARDA_TOKEN_URL, Settings
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
        if self.search == "not-json":  # a proxy's error page, say
            return httpx.Response(200, text="<html>secret-under-test</html>")
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

    fake.search = "not-json"  # a bounded error that never carries the body
    with pytest.raises(field_id.VardaError) as refused:
        client.search(square)
    assert str(refused.value) == "field-searches: the response is not JSON"


@pytest.mark.parametrize(
    ("setting", "url"),
    [
        ("varda_token_url", "https://127.0.0.1:8443/oauth/token"),  # loopback
        ("varda_token_url", "https://169.254.169.254/latest/meta-data/"),  # link-local
        ("varda_token_url", "https://10.0.0.5/oauth/token"),  # private
        ("varda_token_url", "https://attacker.example/oauth/token"),  # another host
        ("varda_token_url", "https://auth.varda.ag.attacker.example/oauth/token"),
        ("varda_token_url", "http://auth.varda.ag/oauth/token"),  # not https
        ("varda_token_url", "https://auth.varda.ag:8443/oauth/token"),  # another port
        ("varda_token_url", "https://auth.varda.ag/elsewhere"),  # another path
        ("varda_api_base_url", "https://localhost/fid/v1"),
        ("varda_api_base_url", "https://api.varda.ag/fid/v2"),
    ],
)
def test_varda_urls_are_pinned_to_its_documented_hosts(setting, url):
    secrets = {
        "varda_client_id": SecretStr("client-under-test"),
        "varda_client_secret": SecretStr("secret-under-test"),
    }
    assert Settings(_env_file=None, **secrets).varda_token_url == DEFAULT_VARDA_TOKEN_URL

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, **secrets, **{setting: url})

    assert "secret-under-test" not in str(refused.value)


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


def test_an_advisor_or_an_agent_token_cannot_trigger_a_lookup(
    session, session_factory, settings, farms
):
    farm, parcel = farms[0], farms[2]
    clock.set_live(session, settings)
    owner = LocalIdentity(login="gfid-owner", password_hash=DUMMY_HASH)
    advisor = LocalIdentity(login="gfid-advisor", password_hash=DUMMY_HASH)
    session.add_all([owner, advisor])
    session.flush()
    session.add_all(
        [
            FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"),
            FarmMembership(identity_id=advisor.id, farm_id=farm.id, role="advisor"),
        ]
    )
    cookie, nonce = "synthetic-gfid-advisor-session", "synthetic-gfid-advisor-csrf"
    session.add(
        LocalSession(
            token_hash=token_hash(cookie),
            identity_id=advisor.id,
            csrf_hash=token_hash(nonce),
            expires_at=datetime.now(UTC) + timedelta(hours=12),
        )
    )
    _, agent_token = share_service.create_token(
        _repo(session, farm),
        creator_id=owner.id,
        name="Synthetic agent",
        scopes=["register:read"],
        purpose="gfid denial test",
        valid_to=datetime.now(UTC) + timedelta(days=7),
        tz="Europe/Paris",
    )
    session.commit()
    fake = FakeVarda()
    app = create_app(settings, manifests=[FIELDS], session_factory=session_factory)
    app.dependency_overrides[field_id.get_client] = lambda: client_for(fake)
    routes = (
        f"/fields/parcels/{parcel.id}/field-id/lookup",
        f"/fields/parcels/{parcel.id}/field-id",
    )

    with TestClient(app, base_url="https://testserver") as web:
        web.cookies.set("farmcore_session", cookie)
        web.cookies.set("farmcore_csrf", nonce)
        advisor_headers = {"Origin": "https://testserver", "X-CSRF-Token": nonce}
        page = web.get(f"/fields/parcels/{parcel.id}", params={"farm": farm.id})
        assert page.status_code == 200 and "Look up Global FieldID" not in page.text
        for route in routes:
            refused = web.post(
                route,
                params={"farm": farm.id},
                data={"field_id": "15RJ.CZM7"},
                headers=advisor_headers,
            )
            assert refused.status_code == 403, route
        web.cookies.clear()
        for route in routes:
            refused = web.post(
                route,
                params={"farm": farm.id},
                data={"field_id": "15RJ.CZM7"},
                headers={"Authorization": f"Bearer {agent_token}"},
            )
            assert refused.status_code == 403, route
            assert "secret-under-test" not in refused.text

    assert fake.calls == []
    assert _kept(session, parcel) == set()
