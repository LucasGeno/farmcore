"""`farmcore mcp --http`: a remote agent uses the farm's tools over MCP Streamable HTTP.

Failure model (one owning test each; what the tools do with a token is owned by
test_mcp_stdio.py, the token itself by tests/apps/share/test_tokens.py):
- the server listens without the installation turning it on (CLI refusal), or beyond loopback
  when nobody configured a host (journey);
- a request without a token, with a wrong one, or with the token in the URL reaches MCP
  (refusal table: 401, and 400 for any query string, so no client learns to put it there);
- DNS rebinding: a page on a foreign Origin, or a request for a foreign Host, reaches MCP with a
  valid token (refusal table: 403, 421); a configured Origin is honoured; CORS stays closed;
- an oversized body is parsed (413), a GET opens a stream that outlives its request (405: the
  server is stateless and answers JSON, so no response stream exists to hold or abuse);
- one token, or one address, floods the server and the database (rate test: 429 per token and
  per IP, independently);
- the server uses one process-wide token instead of each request's own (journey: two tokens with
  different scopes on one server);
- a revoked token keeps working on a running server (journey);
- the token lands in the server's logs, from a header or a URL (journey);
- every HTTP call writes the token row (`last_used_at` is written at most once a minute:
  tests/apps/share/test_tokens.py).
Not committed: slow or idle connections. Nothing here times out a half-sent or silent request:
`MCP_HTTP_MAX_CONNECTIONS` of them hold every slot, so beyond loopback a buffering reverse proxy
with header and body timeouts is required (SECURITY.md).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import Implementation
from sqlalchemy import select
from typer.testing import CliRunner

from farmcore.cli import cli
from farmcore.core import mcp_http, registry
from farmcore.core.models.canonical import AccessLog, DataGrant, Parcel
from farmcore.db import make_session_factory
from farmcore.settings import get_settings
from tests.core.test_mcp_stdio import SQUARE, _token

WAIT = 30.0  # seconds; bounds a hung server, never waited out
ORIGIN = "http://localhost:3000"  # the one browser origin these servers allow
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
ACCEPT = "application/json, text/event-stream"


class Server:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self.lines: list[str] = []
        self.url = ""
        self._ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line)
            found = re.search(r"Uvicorn running on (http://\S+:\d+)", line)
            if found:
                self.url = found.group(1) + "/mcp"
                self._ready.set()
        self._ready.set()

    @property
    def log(self) -> str:
        return "".join(self.lines)


@contextmanager
def _serve(settings, **env: str) -> Iterator[Server]:
    """`farmcore mcp --http` in its own process on a free loopback port, with `env` settings."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "from farmcore.cli import main; main()", "mcp", "--http"],
        env={
            **os.environ,
            "DATABASE_URL": settings.database_url,
            "MCP_HTTP_ENABLED": "true",
            "MCP_HTTP_PORT": "0",
            "MCP_HTTP_ALLOWED_ORIGINS": json.dumps([ORIGIN]),
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # one log: uvicorn's access log goes to stdout
        text=True,
    )
    server = Server(proc)
    try:
        assert server._ready.wait(WAIT) and server.url, server.log
        yield server
    finally:
        proc.terminate()
        proc.wait(WAIT)


@pytest.fixture(scope="module")
def server(settings) -> Iterator[Server]:
    with _serve(settings) as running:
        yield running


def _post(url: str, raw: str | None = None, *, body: dict = INITIALIZE, **headers: str):
    if raw is not None:
        headers["Authorization"] = f"Bearer {raw}"
    return httpx.post(url, json=body, headers={"Accept": ACCEPT, **headers}, timeout=WAIT)


def test_the_server_refuses_to_start_unless_the_installation_turns_it_on(
    settings, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.delenv("MCP_HTTP_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["mcp", "--http"])
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 2, result.output
    assert "MCP_HTTP_ENABLED" in result.output


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("no-token", 401),
        ("wrong-token", 401),
        ("token-in-query", 400),
        ("token-in-query-trailing-slash", 404),
        ("foreign-origin", 403),
        ("foreign-host", 421),
        ("oversized-body", 413),
        ("get-stream", 405),
        ("cors-preflight", 405),
        ("allowed-origin", 200),
    ],
)
def test_a_request_reaches_mcp_only_with_a_bearer_token_from_an_allowed_origin(
    server, committed_session_factory, case, status
):
    with committed_session_factory() as session:
        _farm, _id, raw = _token(session, ["fields:read"])
        session.commit()
    url = server.url
    if case == "no-token":
        response = _post(url)
    elif case == "wrong-token":
        response = _post(url, "fc_pat_" + "A" * 43)
    elif case == "token-in-query":
        response = _post(f"{url}?access_token={raw}")
    elif case == "token-in-query-trailing-slash":
        # A redirect to /mcp would echo the token in its Location header.
        response = _post(f"{url}/?access_token={raw}")
    elif case == "foreign-origin":
        response = _post(url, raw, Origin="http://rebind.example")
    elif case == "foreign-host":
        response = _post(url, raw, Host="rebind.example")
    elif case == "oversized-body":
        big = {**INITIALIZE, "params": {**INITIALIZE["params"], "pad": "x" * 70_000}}
        response = _post(url, raw, body=big)
    elif case == "get-stream":
        response = httpx.get(url, headers={"Authorization": f"Bearer {raw}", "Accept": ACCEPT})
    elif case == "cors-preflight":
        response = httpx.options(
            url,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
    else:
        response = _post(url, raw, Origin=ORIGIN)

    assert response.status_code == status, response.text
    assert "access-control-allow-origin" not in response.headers
    if status == 401:
        assert response.headers["www-authenticate"].startswith("Bearer")


@contextmanager
def _serve_here(settings, engine) -> Iterator[str]:
    """The same app on uvicorn in a thread of this process (no import cost), on a free port."""
    app = mcp_http.create_app(make_session_factory(engine), settings, registry.discover())
    running = uvicorn.Server(uvicorn.Config(app, port=0, access_log=False, log_level="warning"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    try:
        with anyio.from_thread.start_blocking_portal() as portal:
            portal.call(_until_started, running)
        port = running.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        running.should_exit = True
        thread.join(WAIT)


async def _until_started(running: uvicorn.Server) -> None:
    with anyio.fail_after(WAIT):
        while not running.started:
            await anyio.sleep(0.01)


def test_one_token_or_one_address_over_its_rate_gets_429(
    settings, engine, committed_session_factory
):
    with committed_session_factory() as session:
        _farm, _id, first = _token(session, ["fields:read"])
        _farm, _id, second = _token(session, ["fields:read"], farm_name="Second farm")
        session.commit()
    # 2 requests a minute per token, 4 per address; this test sends 5 within milliseconds.
    limited = settings.model_copy(update={"mcp_http_rate_per_token": 2, "mcp_http_rate_per_ip": 4})

    with _serve_here(limited, engine) as url:
        statuses = [_post(url, first).status_code for _ in range(3)]
        other_token = _post(url, second)
        address_spent = _post(url, second)

    assert statuses == [200, 200, 429]
    assert other_token.status_code == 200  # the first token's limit is its own
    assert address_spent.status_code == 429  # five requests from one address
    assert int(address_spent.headers["retry-after"]) > 0


def test_the_official_client_lists_and_calls_tools_over_http_as_each_requests_token(
    server, committed_session_factory
):
    with committed_session_factory() as session:
        farm, token_id, raw = _token(session, ["fields:read", "logs:read"])
        other, _, other_raw = _token(session, ["logs:read"], farm_name="Another farm")
        session.add(
            Parcel(farm_id=other.id, name="Only B", alias="P9", area_ha=Decimal("1"), geom=SQUARE)
        )
        session.commit()

    def connect(token: str) -> Client:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=WAIT)
        return Client(
            streamable_http_client(server.url, http_client=http),
            client_info=Implementation(name="owner", version="1"),
            cache=None,
        )

    async def journey():
        with anyio.fail_after(WAIT):
            async with connect(raw) as client, connect(other_raw) as other_client:
                listed = {t.name for t in (await client.list_tools()).tools}
                other_listed = {t.name for t in (await other_client.list_tools()).tools}
                parcels = await client.call_tool("fields_list_parcels", {})
                with committed_session_factory() as session:
                    grant = session.scalar(select(DataGrant).where(DataGrant.farm_id == farm.id))
                    grant.revoked_at = datetime.now(UTC)
                    session.commit()
                try:
                    revoked = await client.call_tool("fields_list_parcels", {})
                except MCPError as exc:
                    revoked = exc
                return listed, other_listed, parcels, revoked

    listed, other_listed, parcels, revoked = anyio.run(journey)
    in_url = _post(f"{server.url}?token={other_raw}")

    assert server.url.startswith("http://127.0.0.1:")  # loopback unless configured
    assert "fields_list_parcels" in listed
    assert "fields_list_parcels" not in other_listed  # each request's own token, not the first
    assert not parcels.is_error
    assert [p["alias"] for p in parcels.structured_content["facts"]["parcels"]] == ["P1"]
    assert isinstance(revoked, MCPError) or revoked.is_error
    assert in_url.status_code == 400
    with committed_session_factory() as session:
        audited = session.execute(
            select(AccessLog.actor, AccessLog.resource).where(AccessLog.farm_id == farm.id)
        ).all()
    assert {actor for actor, _ in audited} == {f"token:{token_id}"}
    assert [r for _, r in audited].count("mcp:fields_list_parcels") == 1  # revoked ran nothing
    assert raw not in server.log
    assert other_raw not in server.log
