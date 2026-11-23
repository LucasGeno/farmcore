"""Authorization requests, codes and connections (OAuth 2.1 with PKCE, RFC 8707, RFC 9207).

A connection is what an owner's consent creates once its code is exchanged: a `data_grant` of
the approved scopes on the approved farm, an `access_token` row of kind `oauth` (the agent's
audit actor, `token:<id>`, bound to the client and the resource) and rotating secrets in
`oauth_token`. Sharing shows and revokes it like any agent token; revoking the grant ends it.

Every secret (code, access token, refresh token) is 256 random bits shown once and stored as
its SHA-256 only, like personal tokens. Every expiry is set and checked with this server's wall
clock, and tokens are opaque, so no other party's clock is ever trusted.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from farmcore.core.access import (
    GRANT_READ_SCOPES,
    REFRESH_PREFIX,
    TOKEN_BYTES,
    TOKEN_MAX_LEN,
    TOKEN_PREFIXES,
    TOKEN_SCOPES,
    grant_usable,
    is_farm_owner,
)
from farmcore.core.identity import token_hash
from farmcore.core.models.canonical import DataGrant
from farmcore.core.models.identity import AccessToken, OAuthCode, OAuthToken
from farmcore.core.oauth.clients import Client
from farmcore.core.timeline import post_event
from farmcore.settings import Settings

CODE_PREFIX = "fc_oac_"
# RFC 6749 §4.1.2 recommends at most 10 minutes; a client exchanges its code at once.
CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)  # assumed: how long a leaked access token stays useful
CONNECTION_TTL = timedelta(days=90)  # assumed product policy: the owner approves again then
# RFC 7636 §4.2: an S256 challenge is BASE64URL(SHA256(verifier)) without padding, 43 chars.
S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
STATE_MAX = 512
# RFC 6749 Appendix A.5: state is VSCHAR (printable ASCII), so the authorize URL carried to
# sign-in and back (`next`) always fits.
VSCHAR = re.compile(r"[\x20-\x7e]+")
AGENT_NAME_MAX = 80
SOURCE_APP = "core"
# What each scope lets an agent do, in the owner's words (the consent page).
SCOPE_TEXT = {
    "fields:read": "See your fields (parcels) and their crops",
    "stations:read": "See your weather stations' readings",
    "logs:read": "See your spray logs and other farm records",
    "register:read": "See your spray register",
    "advice:read": "See spray advice and disease risk",
    "planning:read": "See your planned jobs",
    "drafts:propose": "Propose new records as drafts you confirm in FarmCore (it cannot save "
    "anything itself)",
}


class Refused(Exception):
    """An authorization request answered with an error, never a code (RFC 6749 §4.1.2.1).
    `redirect`: the client and its `redirect_uri` were verified, so the error may go back to it;
    otherwise it is shown here and nobody is redirected."""

    def __init__(self, error: str, description: str, *, redirect: bool = True) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect = redirect


@dataclass(frozen=True)
class AuthorizationRequest:
    client: Client
    redirect_uri: str
    state: str | None
    scopes: tuple[str, ...]
    code_challenge: str
    resource: str

    def form(self) -> dict[str, str]:
        """The request as the consent form carries it back (re-checked on submit)."""
        return {
            "response_type": "code",
            "client_id": self.client.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "state": self.state or "",
            "scope": " ".join(self.scopes),
            "resource": self.resource,
        }


def valid_state(state: str) -> bool:
    return len(state) <= STATE_MAX and VSCHAR.fullmatch(state) is not None


# What a request naming no scope asks for, and what the MCP 401 tells a client to ask for. Never
# `advice:read` or `drafts:propose`: an owner grants those explicitly.
DEFAULT_SCOPES = GRANT_READ_SCOPES - {"advice:read"}


def parse_request(
    settings: Settings, params: Mapping[str, Any], lookup: Callable[[str], Client | None]
) -> AuthorizationRequest:
    """Check an authorization request (RFC 6749 §4.1.1): a known client (`lookup`, see
    `clients.resolve`), one of its `redirect_uri`s exactly (OAuth 2.1 §4.1.1: no prefix or
    pattern; only a loopback one's port may differ, RFC 8252 §7.3), `response_type=code`, an
    S256 PKCE challenge (`plain` is a downgrade), this installation's MCP endpoint as the
    resource (RFC 8707 §2) and scopes an agent may hold (default: read only). `Refused`
    otherwise."""

    def text(name: str) -> str:
        value = params.get(name)
        return value if isinstance(value, str) else ""

    found = lookup(text("client_id"))
    if found is None:
        raise Refused(
            "invalid_request",
            "This client is not registered here, or its metadata could not be read.",
            redirect=False,
        )
    redirect_uri = text("redirect_uri")
    if not found.accepts(redirect_uri):
        raise Refused(
            "invalid_request",
            "This redirect URI is not registered for the client.",
            redirect=False,
        )
    state = text("state") or None
    if state is not None and not valid_state(state):
        raise Refused("invalid_request", "The state is too long or not printable ASCII.")
    if text("response_type") != "code":
        raise Refused("unsupported_response_type", "Only the authorization code flow is served.")
    if text("code_challenge_method") != "S256" or not S256_CHALLENGE.fullmatch(
        text("code_challenge")
    ):
        raise Refused("invalid_request", "PKCE with an S256 code challenge is required.")
    if text("resource") != settings.mcp_resource_url:
        raise Refused("invalid_target", "Tokens are issued only for this installation's MCP.")
    requested = text("scope").split()
    scopes = tuple(sorted(set(requested) or DEFAULT_SCOPES))
    if not set(scopes) <= TOKEN_SCOPES:
        raise Refused("invalid_scope", "An agent may only read or propose drafts.")
    return AuthorizationRequest(
        found, redirect_uri, state, scopes, text("code_challenge"), text("resource")
    )


def answer_url(settings: Settings, redirect_uri: str, **params: str | None) -> str:
    """`redirect_uri` with the response `params` and this server's `iss` (RFC 9207 §2: on
    every authorization response, success or error, so a client talking to several servers
    can tell which one answered)."""
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in params.items() if v is not None]
    query.append(("iss", settings.oauth_issuer))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _secret(prefix: str) -> tuple[str, str]:
    raw = prefix + secrets.token_urlsafe(TOKEN_BYTES)
    return raw, token_hash(raw)


def issue_code(
    session: Session,
    request: AuthorizationRequest,
    *,
    farm_id: int,
    identity_id: int,
    scopes: list[str],
    grantee: str,
) -> str:
    """A single-use code for what the owner approved; shown only in the redirect."""
    raw, digest = _secret(CODE_PREFIX)
    session.add(
        OAuthCode(
            code_hash=digest,
            client_id=request.client.client_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            resource=request.resource,
            farm_id=farm_id,
            created_by_identity_id=identity_id,
            scopes=sorted(scopes),
            grantee=grantee,
            expires_at=datetime.now(UTC) + CODE_TTL,
        )
    )
    session.flush()
    return raw


def _lookup[M: (OAuthCode, OAuthToken)](
    session: Session, model: type[M], raw: str, prefix: str
) -> M | None:
    if not isinstance(raw, str) or len(raw) > TOKEN_MAX_LEN or not raw.startswith(prefix):
        return None
    column = model.code_hash if model is OAuthCode else model.token_hash
    return session.scalar(select(model).where(column == token_hash(raw)).with_for_update())


def find_code(session: Session, raw: str) -> OAuthCode | None:
    """The code `raw`, used or not, so the token endpoint checks its client, `redirect_uri` and
    verifier first: a replay with wrong parameters must not end anything (OAuth 2.1 §7.5.3, a
    denial of service by anyone who saw the callback URL). `exchange_code` acts on a replay."""
    return _lookup(session, OAuthCode, raw, CODE_PREFIX)


def exchange_code(session: Session, raw: str, client_id: str) -> dict[str, Any] | None:
    """Spend code `raw` (client, `redirect_uri` and PKCE already checked by the token endpoint):
    the connection it approves and its first tokens. Only one exchange wins; None
    (`invalid_grant`) for a used, expired or foreign code, or an approver who is no longer the
    farm's owner. A used code presented with valid parameters is a replay: the connection it
    created ends (RFC 6749 §4.1.2), also when two exchanges race."""
    now = datetime.now(UTC)
    code = session.scalar(
        update(OAuthCode)
        .where(
            OAuthCode.code_hash == token_hash(raw),
            OAuthCode.client_id == client_id,
            OAuthCode.used_at.is_(None),
            OAuthCode.expires_at > now,
        )
        .values(used_at=now)
        .returning(OAuthCode)
    )
    if code is None:
        used = session.scalar(
            select(OAuthCode).where(
                OAuthCode.code_hash == token_hash(raw),
                OAuthCode.client_id == client_id,
                OAuthCode.used_at.is_not(None),
            )
        )
        if used is not None and used.grant_id is not None:
            end_connection(session, used.grant_id, "an authorization code was used twice")
        return None
    if not is_farm_owner(session, code.created_by_identity_id, code.farm_id):
        return None
    grant = DataGrant(
        farm_id=code.farm_id,
        grantee=code.grantee,
        scopes=list(code.scopes),
        purpose=f"MCP agent over OAuth ({client_id})",
        valid_from=now,
        valid_to=now + CONNECTION_TTL,
    )
    session.add(grant)
    session.flush()
    connection = AccessToken(
        kind="oauth",
        token_hash=None,
        grant_id=grant.id,
        created_by_identity_id=code.created_by_identity_id,
        client_id=client_id,
        audience=code.resource,
    )
    session.add(connection)
    code.grant_id = grant.id
    session.flush()
    post_event(
        session,
        farm_id=grant.farm_id,
        res_model="data_grant",
        res_id=grant.id,
        kind="share.granted",
        body=f"Connected the agent {grant.grantee} over OAuth: {', '.join(grant.scopes)}",
        payload={"grantee": grant.grantee, "scopes": grant.scopes, "token": "oauth"},
        actor=f"user:{code.created_by_identity_id}",
        source_app=SOURCE_APP,
        occurred_at=now,
    )
    return _tokens(session, grant, now)


def _tokens(session: Session, grant: DataGrant, now: datetime) -> dict[str, Any]:
    access, access_digest = _secret(TOKEN_PREFIXES["oauth"])
    refresh, refresh_digest = _secret(REFRESH_PREFIX)
    session.add_all(
        [
            OAuthToken(
                token_hash=access_digest,
                kind="access",
                grant_id=grant.id,
                expires_at=now + ACCESS_TTL,
            ),
            OAuthToken(
                token_hash=refresh_digest,
                kind="refresh",
                grant_id=grant.id,
                expires_at=grant.valid_to,
            ),
        ]
    )
    session.flush()
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "scope": " ".join(sorted(grant.scopes)),
        "refresh_token": refresh,
    }


@dataclass(frozen=True)
class Connection:
    token: AccessToken
    grant: DataGrant
    secret: OAuthToken


def _connection(session: Session, secret: OAuthToken) -> Connection | None:
    token = session.scalar(
        select(AccessToken).where(
            AccessToken.grant_id == secret.grant_id, AccessToken.kind == "oauth"
        )
    )
    grant = session.get(DataGrant, secret.grant_id, populate_existing=True)
    if token is None or grant is None:
        return None
    return Connection(token, grant, secret)


def find_refresh(session: Session, raw: str) -> Connection | None:
    """The live connection refresh token `raw` belongs to. A rotated one is a reuse, so a
    sign it leaked (OAuth 2.1 §4.3.1): the connection ends and nothing is returned."""
    secret = _lookup(session, OAuthToken, raw, REFRESH_PREFIX)
    if secret is None or secret.kind != "refresh":
        return None
    if secret.revoked_at is not None:
        end_connection(session, secret.grant_id, "a refresh token was used twice")
        return None
    found = _connection(session, secret)
    if (
        found is None
        or secret.expires_at <= datetime.now(UTC)
        or not grant_usable(session, found.grant, found.token.created_by_identity_id)
    ):
        return None
    return found


def rotate(session: Session, connection: Connection) -> dict[str, Any] | None:
    """New tokens for a refresh: the refresh token and every access token before it stop
    working. None when another refresh won the race (a reuse: the connection ends)."""
    now = datetime.now(UTC)
    spent = session.scalar(
        update(OAuthToken)
        .where(OAuthToken.id == connection.secret.id, OAuthToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(OAuthToken.id)
    )
    if spent is None:
        end_connection(session, connection.grant.id, "a refresh token was used twice")
        return None
    session.execute(
        update(OAuthToken)
        .where(
            OAuthToken.grant_id == connection.grant.id,
            OAuthToken.kind == "access",
            OAuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return _tokens(session, connection.grant, now)


def find_token(session: Session, raw: str) -> Connection | None:
    """The connection an access or refresh token belongs to, whatever its state (RFC 7009)."""
    for prefix in (TOKEN_PREFIXES["oauth"], REFRESH_PREFIX):
        secret = _lookup(session, OAuthToken, raw, prefix)
        if secret is not None:
            return _connection(session, secret)
    return None


def end_connection(session: Session, grant_id: int, why: str) -> None:
    """Revoke a connection's grant and every token of it, once (an event says why)."""
    now = datetime.now(UTC)
    session.execute(
        update(OAuthToken)
        .where(OAuthToken.grant_id == grant_id, OAuthToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    grant = session.get(DataGrant, grant_id, with_for_update=True)
    if grant is None or grant.revoked_at is not None:
        return
    grant.revoked_at = now
    token = session.scalar(select(AccessToken).where(AccessToken.grant_id == grant_id))
    session.flush()
    post_event(
        session,
        farm_id=grant.farm_id,
        res_model="data_grant",
        res_id=grant.id,
        kind="share.revoked",
        body=f"Ended the agent {grant.grantee}'s OAuth connection: {why}",
        payload={"grantee": grant.grantee, "token": "oauth"},
        actor=f"token:{token.id}" if token is not None else "oauth",
        source_app=SOURCE_APP,
        occurred_at=now,
    )
