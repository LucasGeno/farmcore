"""The authorization server's endpoints on the web app (404 unless `MCP_OAUTH_ENABLED`).

- `GET /.well-known/oauth-authorization-server`: metadata (RFC 8414 §3), with
  `authorization_response_iss_parameter_supported` (RFC 9207 §3).
- `GET /oauth/authorize`: checks the request (`service.parse_request`); an unknown client or
  `redirect_uri` gets a 400 page and no redirect, other errors go back to the client with
  `iss`. Signed out: sign in first (back here after). Signed in: the consent page, listing the
  client, where it sends you back, and each requested scope, ticked.
- `POST /oauth/authorize`: the owner's answer. CSRF-checked (the app's `require_csrf`), signed
  in, a current owner of the chosen farm, not a tour; the request is checked again. Approving
  issues a code for the ticked scopes that were requested (never more), denying or ticking none
  answers `access_denied`; both redirect with `iss`.
- `POST /oauth/token`: the MCP SDK's token endpoint (PKCE S256, client and `redirect_uri`
  checks, RFC 6749 error bodies, `no-store`) over `service`: codes and refresh tokens.
- `POST /oauth/revoke`: RFC 7009; revoking either token ends the connection. Always 200.
- `POST /oauth/register`: open Dynamic Client Registration (RFC 7591) for public clients, within
  limits (`clients.register`). Clients may instead use an https URL to their metadata document
  as `client_id` (CIMD), fetched by `app.state.oauth_fetch` (`fetch.MetadataFetcher`).

Nothing here logs a code or a token, and none travels in a URL except the code in its redirect.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from functools import partial
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core.access import OWNER, TOKEN_SCOPES, is_farm_owner
from farmcore.core.identity import principal_for
from farmcore.core.models.canonical import Farm
from farmcore.core.oauth import clients, service
from farmcore.core.oauth.fetch import Fetched
from farmcore.core.tour import is_tour
from farmcore.db import session_scope
from farmcore.settings import Settings
from farmcore.web import templates

router = APIRouter(include_in_schema=False)

METADATA_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REVOKE_PATH = "/oauth/revoke"
REGISTER_PATH = "/oauth/register"
# Reached without a signed-in member (PrivacyMiddleware): clients, and owners about to sign in.
PUBLIC_PATHS = frozenset({METADATA_PATH, AUTHORIZE_PATH, TOKEN_PATH, REVOKE_PATH, REGISTER_PATH})
REGISTRATION_MAX_BYTES = 5 * 1024  # assumed: as much as a metadata document (CIMD §10.11)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# OAuth 2.1 §7.11: the consent page must never be framed (clickjacking).
NO_FRAMING = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.mcp_oauth_enabled:
        raise HTTPException(404)
    return settings


@router.get(METADATA_PATH)
def metadata(request: Request) -> JSONResponse:
    settings = _settings(request)
    issuer = settings.oauth_issuer
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": issuer + AUTHORIZE_PATH,
            "token_endpoint": issuer + TOKEN_PATH,
            "revocation_endpoint": issuer + REVOKE_PATH,
            "registration_endpoint": issuer + REGISTER_PATH,
            "client_id_metadata_document_supported": True,
            "scopes_supported": sorted(TOKEN_SCOPES),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        }
    )


def _refused(settings: Settings, exc: service.Refused, params: Any) -> Response:
    if not exc.redirect:
        return HTMLResponse(
            f"<!doctype html><title>Authorization refused</title><p>{exc.error}: "
            f"{exc.description}</p>",
            status_code=400,
            headers=NO_STORE,
        )
    state = params.get("state")
    url = service.answer_url(
        settings,
        params["redirect_uri"],
        error=exc.error,
        error_description=exc.description,
        # Echoed only when it is a state this server accepts (never an oversized one).
        state=state if isinstance(state, str) and service.valid_state(state) else None,
    )
    return RedirectResponse(url, status_code=302, headers=NO_STORE)


def _owned_farms(session: Session, identity_id: int, farm_ids: list[int]) -> list[Farm]:
    farms = session.scalars(select(Farm).where(Farm.id.in_(farm_ids)).order_by(Farm.id)).all()
    return [f for f in farms if is_farm_owner(session, identity_id, f.id)]


@router.get(AUTHORIZE_PATH)
def authorize(request: Request) -> Response:
    settings = _settings(request)
    params = request.query_params
    try:
        asked = _parse(request, settings, params)
    except service.Refused as exc:
        return _refused(settings, exc, params)
    principal = principal_for(request)
    if principal is None:
        here = f"{AUTHORIZE_PATH}?{urlencode(asked.form())}"
        return RedirectResponse(f"/login?{urlencode({'next': here})}", status_code=303)
    with request.app.state.session_factory() as session:
        farms = _owned_farms(session, principal.identity_id, list(principal.memberships))
    if not farms:
        raise HTTPException(403, "Only a farm owner can connect an agent.")
    page = templates.TemplateResponse(
        request,
        "core/oauth_consent.html",
        {
            "asked": asked,
            "form": asked.form(),
            "farms": farms,
            "returns_to": urlsplit(asked.redirect_uri).hostname,
            "returns_here": clients.loopback_redirect(asked.redirect_uri),
            "scope_text": service.SCOPE_TEXT,
            "csrf_token": request.cookies.get("farmcore_csrf", ""),
        },
    )
    page.headers.update(NO_STORE | NO_FRAMING)
    return page


@router.post(AUTHORIZE_PATH)
async def consent(request: Request) -> Response:
    settings = _settings(request)
    form = await request.form()
    try:
        asked = await anyio.to_thread.run_sync(_parse, request, settings, form)
    except service.Refused as exc:
        return _refused(settings, exc, form)
    principal = principal_for(request)
    if principal is None or is_tour(request):
        raise HTTPException(403, "Farm owner required.")
    allowed = sorted({s for s in form.getlist("allow") if s in asked.scopes})
    if form.get("decision") != "approve" or not allowed:
        url = service.answer_url(
            settings, asked.redirect_uri, error="access_denied", state=asked.state
        )
        return RedirectResponse(url, status_code=303, headers=NO_STORE)
    try:
        farm_id = int(str(form.get("farm", "")))
    except ValueError:
        raise HTTPException(403, "Farm owner required.") from None
    name = str(form.get("name", "")).strip()[: service.AGENT_NAME_MAX]
    if not name or name.casefold() == OWNER:
        name = asked.client.client_name
    code = await anyio.to_thread.run_sync(
        partial(
            _issue,
            request.app.state.session_factory,
            asked,
            farm_id,
            principal.identity_id,
            allowed,
            name,
        )
    )
    if code is None:
        raise HTTPException(403, "Farm owner required.")
    url = service.answer_url(settings, asked.redirect_uri, code=code, state=asked.state)
    return RedirectResponse(url, status_code=303, headers=NO_STORE)


def _fetch(request: Request) -> Callable[[str], Fetched]:
    """The metadata fetch, counted against the address that asked (`fetch.MetadataFetcher`)."""
    return partial(
        request.app.state.oauth_fetch, peer=request.client.host if request.client else ""
    )


def _parse(request: Request, settings: Settings, params: Any) -> service.AuthorizationRequest:
    """`service.parse_request`, its client looked up (and its metadata fetched) in a session."""
    with session_scope(request.app.state.session_factory) as session:
        try:
            return service.parse_request(
                settings, params, partial(clients.resolve, session, settings, _fetch(request))
            )
        except service.Refused:
            # What the lookup learnt stands (connections ended, a failure's start): only the
            # request is refused.
            session.commit()
            raise


def _issue(
    factory: sessionmaker[Session],
    asked: service.AuthorizationRequest,
    farm_id: int,
    identity_id: int,
    scopes: list[str],
    name: str,
) -> str | None:
    with session_scope(factory) as session:
        # Serialise with membership edits, then re-read authority (as `require_farm_owner`).
        if session.scalar(select(Farm.id).where(Farm.id == farm_id).with_for_update()) is None:
            return None
        if not is_farm_owner(session, identity_id, farm_id):
            return None
        return service.issue_code(
            session, asked, farm_id=farm_id, identity_id=identity_id, scopes=scopes, grantee=name
        )


class Provider:
    """The MCP SDK's view of `service` (its `OAuthAuthorizationServerProvider`, token side)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        fetch: Callable[[str], Fetched],
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._fetch = fetch

    async def _run[T](self, work: Callable[[Session], T]) -> T:
        def scoped() -> T:
            with session_scope(self._factory) as session:
                return work(session)

        return await anyio.to_thread.run_sync(scoped)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        found = await self._run(
            partial(
                clients.resolve, settings=self._settings, fetch=self._fetch, client_id=client_id
            )
        )
        if found is None:
            return None
        return OAuthClientInformationFull(
            client_id=found.client_id,
            client_name=found.client_name,
            redirect_uris=[AnyUrl(u) for u in found.redirect_uris],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def load(session: Session) -> AuthorizationCode | None:
            code = service.find_code(session, authorization_code)
            if code is None:
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=list(code.scopes),
                expires_at=code.expires_at.timestamp(),
                client_id=code.client_id,
                code_challenge=code.code_challenge,
                redirect_uri=AnyUrl(code.redirect_uri),
                redirect_uri_provided_explicitly=True,
                resource=code.resource,
            )

        return await self._run(load)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        tokens = await self._run(
            partial(
                service.exchange_code,
                raw=authorization_code.code,
                client_id=str(client.client_id),
            )
        )
        if tokens is None:
            raise TokenError("invalid_grant", "The authorization code is not valid.")
        return OAuthToken(**tokens)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        def load(session: Session) -> RefreshToken | None:
            found = service.find_refresh(session, refresh_token)
            if found is None or found.token.client_id != client.client_id:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=str(client.client_id),
                scopes=list(found.grant.scopes),
                expires_at=int(found.secret.expires_at.timestamp()),
                resource=found.token.audience,
            )

        return await self._run(load)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # A connection's scopes are its grant's: a narrower request still gets the grant's,
        # and the response says so (RFC 6749 §5.1 `scope`).
        def rotate(session: Session) -> dict[str, Any] | None:
            found = service.find_refresh(session, refresh_token.token)
            return None if found is None else service.rotate(session, found)

        tokens = await self._run(rotate)
        if tokens is None:
            raise TokenError("invalid_grant", "The refresh token is not valid.")
        return OAuthToken(**tokens)


@router.post(TOKEN_PATH)
async def token(request: Request) -> Response:
    settings = _settings(request)
    form = await request.form()  # cached: the SDK's handler reads the same form
    resource = form.get("resource")
    if resource is not None and resource != settings.mcp_resource_url:
        # RFC 8707 §2: every code and refresh token here is for this one resource.
        return JSONResponse(
            {"error": "invalid_target", "error_description": "Unknown resource."},
            status_code=400,
            headers=NO_STORE,
        )
    provider = Provider(request.app.state.session_factory, settings, _fetch(request))
    handler = TokenHandler(provider, ClientAuthenticator(provider))  # type: ignore[arg-type]
    return await handler.handle(request)


@router.post(REVOKE_PATH)
async def revoke(request: Request) -> Response:
    """RFC 7009 §2: a public client names itself. A token issued to another client is refused
    (`unauthorized_client`, §2.1) and stays alive; an unknown or already revoked token gets 200
    (§2.2)."""
    settings = _settings(request)
    form = await request.form()
    raw, client_id = form.get("token"), form.get("client_id")
    if not isinstance(raw, str) or not raw or not isinstance(client_id, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400, headers=NO_STORE)

    def end(session: Session) -> str | None:
        """The error to answer with, or None (done, or nothing to do)."""
        if clients.resolve(session, settings, _fetch(request), client_id) is None:
            return "invalid_client"
        found = service.find_token(session, raw)
        if found is None:
            return None
        if found.token.client_id != client_id:
            return "unauthorized_client"
        service.end_connection(session, found.grant.id, "the agent revoked it")
        return None

    error = await anyio.to_thread.run_sync(
        partial(_in_session, request.app.state.session_factory, end)
    )
    if error is not None:
        status = 401 if error == "invalid_client" else 400
        return JSONResponse({"error": error}, status_code=status, headers=NO_STORE)
    return Response(status_code=200, headers=NO_STORE)


@router.post(REGISTER_PATH)
async def register(request: Request) -> Response:
    """RFC 7591 §3: a public client registers its name and redirect URIs; 201 with its new
    `client_id` and the metadata as registered (§3.2.1), 400 with an RFC 7591 §3.2.2 error, 429
    past a limit. A software statement is ignored (§3.1.1) and nothing else is echoed."""
    _settings(request)  # 404 unless OAuth is on
    body = b""
    async for chunk in request.stream():  # read no more than the cap, whatever is sent
        body += chunk
        if len(body) > REGISTRATION_MAX_BYTES:
            break
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if len(body) > REGISTRATION_MAX_BYTES or media != "application/json":
            raise clients.Unacceptable("invalid_client_metadata", "A small JSON object.")
        try:
            meta = json.loads(body)
        except ValueError:
            raise clients.Unacceptable("invalid_client_metadata", "Not JSON.") from None
        if not isinstance(meta, dict):
            raise clients.Unacceptable("invalid_client_metadata", "Not a JSON object.")
        name, redirect_uris = clients.check_metadata(meta, name_required=False)
    except clients.Unacceptable as exc:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=400,
            headers=NO_STORE,
        )
    peer = request.client.host if request.client else "unknown"
    found = await anyio.to_thread.run_sync(
        partial(
            _in_session,
            request.app.state.session_factory,
            partial(clients.register, peer=peer, name=name, redirect_uris=redirect_uris),
        )
    )
    if found is None:
        return JSONResponse(
            {"error": "too_many_registrations", "error_description": "Try again later."},
            status_code=429,
            headers=NO_STORE | {"Retry-After": "3600"},
        )
    return JSONResponse(
        {
            "client_id": found.client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": found.client_name,
            "redirect_uris": list(found.redirect_uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
        headers=NO_STORE,
    )


def _in_session[T](factory: sessionmaker[Session], work: Callable[[Session], T]) -> T:
    with session_scope(factory) as session:
        return work(session)
