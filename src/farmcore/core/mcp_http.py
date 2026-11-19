"""The farm's tools for a remote agent over MCP Streamable HTTP (`farmcore mcp --http`).

Off unless `MCP_HTTP_ENABLED`; listens on `MCP_HTTP_HOST` (loopback by default) at `/mcp`. The
server is stateless and answers JSON: every message is one POST and its response, so no session
can be taken over and no stream outlives its request. Each request passes, in order, before any
MCP message is read:

1. `POST /mcp` only: a GET stream or a DELETE gets 405 (the transport spec allows it).
2. No query string: a token in a URL lands in proxy and browser logs, so such a request is
   refused (400) without being authenticated. Any other path is 404, never a redirect.
3. `Host` and `Origin` against allowlists (the SDK's check, DNS rebinding): 421 / 403. No CORS
   headers are ever sent.
4. The client address's rate (429): before the database is asked about any token.
5. A bearer token in `Authorization` only (`access.authenticate_token`): 401 otherwise.
6. That token's rate (429), keyed by token, not by address.
7. The body size (413, the SDK's limit).

Each tool list and call then re-authenticates the request's own token (`mcp.FarmTools`) and is
audited as `token:<id>`. Nothing here logs a URL, a header or a body; uvicorn's access log is off
and each refusal logs one line with its status and the client address.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from farmcore.core.access import TokenRejected, authenticate_token
from farmcore.core.manifest import Manifest
from farmcore.core.mcp import FarmTools, server
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

PATH = "/mcp"
LOOPBACK_HOSTS = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*")
MAX_TRACKED = 10_000  # rate buckets kept before idle (full) ones are dropped


def bearer(headers: Headers) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, or None."""
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


class RateLimit:
    """A token bucket per key: `per_minute` requests, refilled evenly over the minute."""

    def __init__(self, per_minute: int, clock=time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def wait(self, key: str) -> int:
        """Take one request for `key`: 0 when allowed, else the seconds until the next one."""
        now = self._clock()
        if len(self._buckets) > MAX_TRACKED:
            self._buckets = {
                k: (level, at)
                for k, (level, at) in self._buckets.items()
                if level + (now - at) * self._rate < self._capacity
            }
        level, at = self._buckets.get(key, (self._capacity, now))
        level = min(self._capacity, level + (now - at) * self._rate)
        if level < 1:
            self._buckets[key] = (level, now)
            return max(1, math.ceil((1 - level) / self._rate))
        self._buckets[key] = (level - 1, now)
        return 0


def _refusal(status: int, message: str, **headers: str) -> Response:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _unauthorized() -> Response:
    return _refusal(
        401,
        "A bearer token issued on Sharing is required.",
        **{"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _too_many(seconds: int) -> Response:
    return _refusal(429, "Too many requests.", **{"Retry-After": str(seconds)})


class Gate:
    """Steps 1-6 of the module docstring, in front of the SDK's Streamable HTTP endpoint."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        mcp: StreamableHTTPSessionManager,
    ) -> None:
        self._session_factory = session_factory
        self._mcp = mcp
        self._origin = TransportSecurityMiddleware(
            TransportSecuritySettings(
                allowed_hosts=settings.mcp_http_allowed_hosts or list(LOOPBACK_HOSTS),
                allowed_origins=settings.mcp_http_allowed_origins,
            )
        )
        self._per_ip = RateLimit(settings.mcp_http_rate_per_ip)
        self._per_token = RateLimit(settings.mcp_http_rate_per_token)

    def _actor(self, token: str) -> str | None:
        with session_scope(self._session_factory) as session:
            try:
                return authenticate_token(session, token).actor
            except TokenRejected:
                return None

    async def _refusal(self, request: Request) -> Response | None:
        if request.method != "POST":
            return _refusal(405, "Only POST.", Allow="POST")
        if request.url.query:
            return _refusal(400, "Send the token in the Authorization header, never the URL.")
        refused = await self._origin.validate_request(request)
        if refused is not None:
            return refused
        client = request.client.host if request.client else ""
        if wait := self._per_ip.wait(client):
            return _too_many(wait)
        token = bearer(request.headers)
        actor = await anyio.to_thread.run_sync(self._actor, token) if token else None
        if actor is None:
            return _unauthorized()
        if wait := self._per_token.wait(actor):
            return _too_many(wait)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        refused = await self._refusal(request)
        if refused is not None:
            # Status and address only: the access log is off, and a URL, header or body may
            # carry a token.
            client = request.client.host if request.client else "?"
            logger.info("mcp http refused %s from %s", refused.status_code, client)
            await refused(scope, receive, send)
            return
        await self._mcp.handle_request(scope, receive, send)


def _token_of(ctx: Any) -> str:
    """The token of the HTTP request carrying this MCP message (the gate checked it)."""
    return bearer(ctx.request.headers) or ""


def create_app(
    session_factory: sessionmaker[Session], settings: Settings, manifests: Sequence[Manifest]
) -> Starlette:
    """The `/mcp` endpoint (run it with uvicorn's access log off)."""
    mcp = StreamableHTTPSessionManager(
        server(FarmTools(session_factory, settings, manifests), _token_of),
        stateless=True,
        json_response=True,
        max_request_body_size=settings.mcp_http_max_body_bytes,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.run():
            yield

    gate = Gate(session_factory, settings, mcp)
    app = Starlette(routes=[Route(PATH, endpoint=gate, methods=None)], lifespan=lifespan)
    # No redirect from /mcp/ to /mcp: its Location would echo the query string.
    app.router.redirect_slashes = False
    return app
