"""OAuth 2.1 for MCP: FarmCore authorizes an agent, on its owner's consent, to call `/mcp`.

Failure model (one owning test each; the MCP gate itself is owned by test_mcp_http.py, what a
token's grant may read by tests/apps/share/test_tokens.py):
- a client cannot discover the authorization server or the resource, or discovers another one
  (metadata test: RFC 8414, RFC 9728 and the 401's `resource_metadata`); OAuth is off unless
  the installation turns it on (metadata test: 404 by default);
- a code reaches an attacker: an unregistered or loosely matched `redirect_uri` (open redirect),
  an unknown client, an implicit grant, PKCE missing or downgraded to `plain` (authorize table);
- mix-up: a client cannot tell which server answered, because an authorization response, success
  or error, lacks `iss` (RFC 9207; asserted on every redirect here);
- audience confusion: a token for another resource, or for this one presented elsewhere (stdio),
  is accepted (journey; RFC 8707), or an authorization request for another resource gets a code
  (authorize table);
- a token leaks: in a redirect URL, a cached response, or the logs (journey);
- consent is forged or hijacked: no CSRF nonce, a member who is not the owner, a farm the signer
  does not own, or scopes the owner did not tick or cannot grant (consent table), or the page is
  framed (clickjacking; journey);
- an intercepted or replayed code, a wrong verifier, another redirect, client or resource, or an
  expired code buys a token; a valid replay leaves its first tokens alive, or a replay with a
  wrong verifier (anyone who saw the callback URL) ends them (token table);
- a refresh token is not rotated, a rotated one still works, its reuse leaves the connection
  alive, revocation does not end it, another client revokes it, an expired access token still
  works (refresh test).
Clock skew: this server issues and checks every expiry with one wall clock and opaque tokens
(no JWT `exp` read by another party), so skew between parties cannot extend a token.
Not committed: real-client runs (later ticket), a browser's SameSite handling of the consent hop.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from farmcore.core import mcp_http, registry
from farmcore.core.access import TokenRejected, authenticate_token, ensure_owner_grant
from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import Farm
from farmcore.core.models.identity import FarmMembership, LocalIdentity, OAuthCode, OAuthToken
from farmcore.core.oauth import clients
from farmcore.core.oauth import service as oauth
from farmcore.main import create_app
from farmcore.settings import OAuthClient

PASSWORD = "synthetic-oauth-owner-password"  # gauntlet:allow-secret synthetic credential
ISSUER = "https://testserver"
RESOURCE = "https://farm.example/mcp"
CLIENT = "test-agent"
OTHER_CLIENT = "other-agent"
REDIRECT = "https://agent.example/callback"
# RFC 7636 Appendix B: the worked example's verifier and its S256 challenge.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
TOKEN = re.compile(r"fc_o[ar]t_[A-Za-z0-9_-]{43}")


@pytest.fixture
def oauth_settings(settings):
    return settings.model_copy(
        update={
            "public_base_url": ISSUER,
            "mcp_http_enabled": True,
            "mcp_oauth_enabled": True,
            "mcp_http_public_url": RESOURCE,
            "mcp_http_allowed_hosts": ["testserver"],
            "mcp_oauth_clients": [
                OAuthClient(client_id=CLIENT, client_name="Test agent", redirect_uris=[REDIRECT]),
                OAuthClient(
                    client_id=OTHER_CLIENT,
                    client_name="Other agent",
                    redirect_uris=["https://other.example/cb"],
                ),
            ],
        }
    )


@pytest.fixture(scope="module")
def credential_hash() -> str:
    return hash_password(PASSWORD)


def _identity(session, farm, login, role, credential_hash) -> LocalIdentity:
    identity = LocalIdentity(login=login, password_hash=credential_hash)
    session.add(identity)
    session.flush()
    session.add(FarmMembership(identity_id=identity.id, farm_id=farm.id, role=role))
    return identity


@pytest.fixture
def farm(session, credential_hash) -> Farm:
    farm = Farm(name="OAuth farm")
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    _identity(session, farm, "oauth-owner", "owner", credential_hash)
    _identity(session, farm, "oauth-advisor", "advisor", credential_hash)
    session.commit()
    return farm


@pytest.fixture
def app(oauth_settings, session_factory, farm):
    return create_app(oauth_settings, session_factory=session_factory)


@pytest.fixture
def agent(app):
    """The MCP client's own HTTP calls: no browser cookies."""
    with TestClient(app, base_url=ISSUER) as client:
        yield client


def _signed_in(app, login: str) -> TestClient:
    browser = TestClient(app, base_url=ISSUER)
    page = browser.get("/login")
    nonce = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    signed = browser.post(
        "/login",
        data={"login": login, "password": PASSWORD, "csrf_token": nonce},
        headers={"Origin": ISSUER},
        follow_redirects=False,
    )
    assert signed.status_code == 303
    return browser


@pytest.fixture
def owner(app):
    with _signed_in(app, "oauth-owner") as browser:
        yield browser


def _request(**over: str) -> dict[str, str]:
    """An authorization request (RFC 6749 §4.1.1, RFC 7636 §4.3, RFC 8707 §2.1)."""
    return {
        "response_type": "code",
        "client_id": CLIENT,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "s1",
        "scope": "fields:read logs:read",
        "resource": RESOURCE,
        **over,
    }


def _consent(browser, home, *, csrf: bool = True, **over) -> object:
    """The owner's answer on the consent page: approve `allow` (default: what was asked)."""
    form = {
        **_request(),
        "farm": str(home.id),
        "name": "My agent",
        "allow": ["fields:read", "logs:read"],
        "decision": "approve",
        **over,
    }
    if csrf:
        form["csrf_token"] = browser.cookies.get("farmcore_csrf", "")
    return browser.post(
        "/oauth/authorize", data=form, headers={"Origin": ISSUER}, follow_redirects=False
    )


def _answer(response) -> dict[str, str]:
    """The query of a redirect back to the client, after checking it goes there."""
    assert response.status_code in (302, 303), response.text
    location = response.headers["location"]
    assert location.startswith(REDIRECT + "?")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _exchange(agent, code: str, **over: str):
    return agent.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": CLIENT,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
            **over,
        },
    )


def _refresh(agent, refresh: str):
    return agent.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT},
    )


def _connect(owner, agent, farm) -> dict:
    """Consent then exchange: the token response."""
    answer = _answer(_consent(owner, farm))
    issued = _exchange(agent, answer["code"])
    assert issued.status_code == 200, issued.text
    return issued.json()


def _accepted(session, raw: str, audience: str | None = RESOURCE) -> bool:
    try:
        authenticate_token(session, raw, audience=audience)
    except TokenRejected:
        return False
    return True


def _no_fetch(url: str):
    raise AssertionError(f"an operator-listed client is never fetched: {url}")


def test_the_401_and_a_request_naming_no_scope_ask_for_the_same_read_scopes(
    oauth_settings, session_factory, session
) -> None:
    """MCP authorization spec, scope selection: the 401 names the scopes a client should ask for
    (RFC 6750 §3 `scope`), so it asks for least privilege instead of every supported scope, and
    an authorization request naming none gets the same set. An owner must choose to let an agent
    read stored advice or propose drafts, so neither is in it."""
    with TestClient(
        mcp_http.create_app(session_factory, oauth_settings, registry.discover())
    ) as mcp:
        unauthorized = mcp.post("/mcp", json={})
    params = {k: v for k, v in _request().items() if k != "scope"}

    asked = oauth.parse_request(
        oauth_settings, params, partial(clients.resolve, session, oauth_settings, _no_fetch)
    )

    assert unauthorized.status_code == 401
    named = re.search(r'(?:^|[ ,])scope="([^"]*)"', unauthorized.headers["www-authenticate"])
    assert named is not None
    assert set(named.group(1).split()) == set(asked.scopes)
    assert "fields:read" in asked.scopes
    assert not {"advice:read", "drafts:propose"} & set(asked.scopes)


def test_metadata_points_a_client_at_this_server_and_this_resource_only_when_on(
    oauth_settings, settings, session_factory, agent
) -> None:
    """RFC 8414 §3 (metadata at the issuer's well-known URI, `issuer` identical), RFC 9207 §3
    (`authorization_response_iss_parameter_supported`), RFC 7636 §4.3 (S256 only), RFC 9728 §3
    (resource metadata at the well-known URI built from the resource) and §5.1 (the 401 names
    it), and the MCP authorization spec's discovery order."""
    server = agent.get("/.well-known/oauth-authorization-server").json()
    manifests = registry.discover()
    with TestClient(mcp_http.create_app(session_factory, oauth_settings, manifests)) as mcp:
        resource = mcp.get("/.well-known/oauth-protected-resource/mcp").json()
        unauthorized = mcp.post("/mcp", json={})
    with TestClient(create_app(settings, session_factory=session_factory)) as off:
        server_off = off.get("/.well-known/oauth-authorization-server")
    with TestClient(mcp_http.create_app(session_factory, settings, manifests)) as mcp_off:
        resource_off = mcp_off.get("/.well-known/oauth-protected-resource/mcp")

    assert server["issuer"] == ISSUER
    assert server["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert server["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert server["revocation_endpoint"] == f"{ISSUER}/oauth/revoke"
    assert server["response_types_supported"] == ["code"]
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["authorization_response_iss_parameter_supported"] is True
    assert "drafts:propose" in server["scopes_supported"]
    assert "logs:write" not in server["scopes_supported"]
    assert resource["resource"] == RESOURCE
    assert resource["authorization_servers"] == [ISSUER]
    assert unauthorized.status_code == 401
    assert (
        'resource_metadata="https://farm.example/.well-known/oauth-protected-resource/mcp"'
        in unauthorized.headers["www-authenticate"]
    )
    assert (server_off.status_code, resource_off.status_code) == (404, 404)


def test_an_owner_connects_an_agent_whose_token_works_only_for_this_resource(
    owner, agent, farm, session, caplog
) -> None:
    """RFC 6749 §4.1 with RFC 7636 §4.6 (S256), RFC 9207 §2 (`iss` on the response), RFC 8707
    §2 (the token is bound to the requested resource and refused for any other), RFC 6749 §5.1
    (`Cache-Control: no-store`) and §10.3/§10.5 (tokens never in a URL or a log), OAuth 2.1
    §7.11 (the consent page refuses to be framed)."""
    caplog.set_level(logging.DEBUG)
    page = owner.get("/oauth/authorize", params=_request())

    answer = _answer(_consent(owner, farm))
    issued = _exchange(agent, answer["code"])

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert 'value="fields:read"' in page.text and 'value="logs:read"' in page.text
    assert "Test agent" in page.text and "agent.example" in page.text
    assert set(answer) == {"code", "state", "iss"}
    assert (answer["state"], answer["iss"]) == ("s1", ISSUER)
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"
    body = issued.json()
    assert body["token_type"] == "Bearer"
    assert sorted(body["scope"].split()) == ["fields:read", "logs:read"]
    assert TOKEN.fullmatch(body["access_token"]) and TOKEN.fullmatch(body["refresh_token"])
    repo = authenticate_token(session, body["access_token"], audience=RESOURCE)
    assert repo.farm_id == farm.id and repo.actor.startswith("token:")
    assert sorted(repo.grant.scopes) == ["fields:read", "logs:read"]
    assert not _accepted(session, body["access_token"], audience="https://other.example/mcp")
    assert not _accepted(session, body["access_token"], audience=None)  # stdio is not /mcp
    assert not _accepted(session, body["refresh_token"])  # a refresh token is no access token
    logged = caplog.text
    assert body["access_token"] not in logged and body["refresh_token"] not in logged
    assert answer["code"] not in logged


@pytest.mark.parametrize(
    ("name", "over", "error"),
    [
        ("unknown-client", {"client_id": "nobody"}, None),
        ("unregistered-redirect", {"redirect_uri": "https://evil.example/callback"}, None),
        ("redirect-not-exact", {"redirect_uri": REDIRECT + "/../steal"}, None),
        ("redirect-extra-query", {"redirect_uri": REDIRECT + "?next=evil"}, None),
        ("implicit-grant", {"response_type": "token"}, "unsupported_response_type"),
        ("pkce-plain", {"code_challenge_method": "plain"}, "invalid_request"),
        ("pkce-missing", {"code_challenge": ""}, "invalid_request"),
        ("pkce-malformed", {"code_challenge": "short"}, "invalid_request"),
        ("other-resource", {"resource": "https://other.example/mcp"}, "invalid_target"),
        ("no-resource", {"resource": ""}, "invalid_target"),
        ("scope-beyond-tokens", {"scope": "fields:read logs:write"}, "invalid_scope"),
    ],
)
def test_an_authorization_request_that_could_leak_a_code_is_refused(
    agent, name, over, error
) -> None:
    """RFC 6749 §4.1.2.1 (no redirect to an unverified client or `redirect_uri`, an error
    redirect otherwise), OAuth 2.1 §2.3.1 and §4.1.1 (exact `redirect_uri` match), RFC 7636
    §4.4.1 and OAuth 2.1 §4.1.1 (S256 only, a 43-128 character challenge), RFC 8707 §2
    (`invalid_target`), RFC 9207 §2 (`iss` on error responses too)."""
    refused = agent.get("/oauth/authorize", params=_request(**over), follow_redirects=False)

    if error is None:
        assert refused.status_code == 400
        assert "location" not in refused.headers
    else:
        answer = _answer(refused)
        assert {k: answer.get(k) for k in ("error", "state", "iss")} == {
            "error": error,
            "state": "s1",
            "iss": ISSUER,
        }
        assert "code" not in answer


@pytest.mark.parametrize(
    ("name", "signer", "over", "outcome"),
    [
        ("no-csrf-nonce", "oauth-owner", {"csrf": False}, 403),
        ("advisor-not-owner", "oauth-advisor", {}, 403),
        ("farm-not-owned", "oauth-owner", {"farm": "other"}, 403),
        ("owner-denies", "oauth-owner", {"decision": "deny"}, "access_denied"),
        ("nothing-ticked", "oauth-owner", {"allow": []}, "access_denied"),
        (
            "ticks-beyond-the-request",
            "oauth-owner",
            {"allow": ["fields:read", "logs:write", "drafts:propose", "*"]},
            ["fields:read"],
        ),
    ],
)
def test_only_the_farms_owner_consents_and_only_to_what_was_asked(
    app, agent, farm, session, name, signer, over, outcome
) -> None:
    """Consent is the owner's, on this farm, CSRF-protected (RFC 6749 §10.12);
    the token holds at most the requested scopes the owner ticked (RFC 6749 §3.3), never more
    than an agent token may hold; a denial is an `access_denied` redirect with `iss`."""
    if over.get("farm") == "other":
        other = Farm(name="Not theirs")
        session.add(other)
        session.flush()
        ensure_owner_grant(session, other)
        session.commit()
        over = {**over, "farm": str(other.id)}
    with _signed_in(app, signer) as browser:
        answered = _consent(browser, farm, **over)

    if outcome == 403:
        assert answered.status_code == 403
        assert session.scalars(select(OAuthCode)).all() == []
    elif isinstance(outcome, str):
        answer = _answer(answered)
        assert {k: answer[k] for k in ("error", "state", "iss")} == {
            "error": outcome,
            "state": "s1",
            "iss": ISSUER,
        }
        assert "code" not in answer
    else:
        issued = _exchange(agent, _answer(answered)["code"])
        assert issued.json()["scope"].split() == outcome


@pytest.mark.parametrize(
    ("name", "over", "error"),
    [
        ("wrong-verifier", {"code_verifier": "x" * 43}, "invalid_grant"),
        ("other-redirect", {"redirect_uri": "https://agent.example/other"}, "invalid_request"),
        ("other-client", {"client_id": OTHER_CLIENT}, "invalid_grant"),
        ("expired", {}, "invalid_grant"),
        ("other-resource", {"resource": "https://other.example/mcp"}, "invalid_target"),
        ("replayed", {}, "invalid_grant"),
        ("replayed-wrong-verifier", {"code_verifier": "x" * 43}, "invalid_grant"),
    ],
)
def test_a_stolen_or_replayed_code_buys_no_token(owner, agent, farm, session, name, over, error):
    """RFC 7636 §4.6 (the verifier must match), RFC 6749 §4.1.3 (same client and
    `redirect_uri`; codes are single-use, and on reuse the tokens already issued from it are
    revoked, §4.1.2), but not on a replay with wrong parameters (OAuth 2.1 §7.5.3), OAuth 2.1
    §4.1.2 (short-lived codes), RFC 8707 §2 (`invalid_target` for another resource)."""
    code = _answer(_consent(owner, farm))["code"]
    first = None
    if name == "expired":
        session.execute(update(OAuthCode).values(expires_at=datetime.now(UTC) - timedelta(1)))
        session.commit()
    if name.startswith("replayed"):
        first = _exchange(agent, code).json()

    refused = _exchange(agent, code, **over)

    assert refused.status_code == 400
    assert refused.json()["error"] == error
    assert "access_token" not in refused.json()
    if first is not None:
        # A valid replay ends the connection; a wrong verifier proves nothing, ends nothing.
        assert _accepted(session, first["access_token"]) == (name != "replayed")


def test_refresh_rotates_and_reuse_revocation_or_expiry_ends_the_connection(
    owner, agent, farm, session
) -> None:
    """OAuth 2.1 §4.3.1 (public clients' refresh tokens rotate; a reused one is a breach, so
    the connection ends), RFC 7009 §2.1-2.2 (revoking ends access and refresh with 200; another
    client's request is refused and ends nothing),
    RFC 6749 §5.1 (`expires_in`): an access token past it is refused."""
    first = _connect(owner, agent, farm)
    rotated = _refresh(agent, first["refresh_token"]).json()
    rotated_works = _accepted(session, rotated["access_token"])
    first_works = _accepted(session, first["access_token"])
    reused = _refresh(agent, first["refresh_token"])

    second = _connect(owner, agent, farm)
    foreign = agent.post(
        "/oauth/revoke", data={"token": second["access_token"], "client_id": OTHER_CLIENT}
    )
    foreign_left_it = _accepted(session, second["access_token"])
    revoked = agent.post(
        "/oauth/revoke", data={"token": second["access_token"], "client_id": CLIENT}
    )

    third = _connect(owner, agent, farm)
    session.execute(
        update(OAuthToken)
        .where(OAuthToken.kind == "access")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    session.commit()

    assert TOKEN.fullmatch(rotated["refresh_token"])
    assert rotated["refresh_token"] != first["refresh_token"]
    assert rotated["expires_in"] > 0
    assert (rotated_works, first_works) == (True, False)
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    assert not _accepted(session, rotated["access_token"])  # reuse ended the connection
    assert _refresh(agent, rotated["refresh_token"]).status_code == 400
    assert foreign.status_code == 400 and foreign.json()["error"] == "unauthorized_client"
    assert foreign_left_it
    assert revoked.status_code == 200
    assert not _accepted(session, second["access_token"])
    assert _refresh(agent, second["refresh_token"]).status_code == 400
    assert not _accepted(session, third["access_token"])  # expired
