"""`farmcore mcp`: serve the farm's read and propose tools to an agent over MCP.

- stdio (default), for a local agent: the agent token comes from `FARMCORE_TOKEN` only (issued
  on Sharing); the command refuses to start without a valid one and never prints it. Stdout
  carries the protocol; logs go to stderr.
- `--http`, for remote agents: Streamable HTTP at `/mcp` on `MCP_HTTP_HOST:MCP_HTTP_PORT`, each
  request with its own token (`core/mcp_http.py`). Refuses to start unless `MCP_HTTP_ENABLED`.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

import anyio
import typer
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import configure_mappers

from farmcore.core import mcp, registry
from farmcore.core.commands.backup import database_error
from farmcore.db import make_engine, make_session_factory
from farmcore.settings import get_settings, is_loopback


def _fail(message: str, code: int = 1) -> None:
    typer.echo(f"mcp: {message}", err=True)
    raise typer.Exit(code)


def _serve_http(session_factory, settings, manifests) -> None:
    import uvicorn

    from farmcore.core import mcp_http

    host = settings.mcp_http_host
    if not is_loopback(host):
        logging.getLogger(__name__).warning(
            "MCP over HTTP listens beyond loopback on %s: serve it only behind a buffering TLS "
            "reverse proxy with header and body timeouts; idle or half-sent connections are "
            "never timed out here.",
            host,
        )
    forwarded = settings.mcp_http_forwarded_allow_ips
    uvicorn.run(
        mcp_http.create_app(session_factory, settings, manifests),
        host=host,
        port=settings.mcp_http_port,
        access_log=False,  # a URL may carry what a client should never have put there
        limit_concurrency=settings.mcp_http_max_connections,
        timeout_keep_alive=5,
        # Trust X-Forwarded-For only from the configured proxy, never implicitly from loopback.
        proxy_headers=bool(forwarded),
        forwarded_allow_ips=forwarded or None,
        log_level="info",
    )


def register(cli: typer.Typer) -> None:
    @cli.command(name="mcp")
    def serve(
        http: bool = typer.Option(
            False, "--http", help="Serve remote agents over Streamable HTTP (MCP_HTTP_*)."
        ),
        app_package: list[str] = typer.Option(
            ["farmcore.apps"], "--app-package", hidden=True, help="App packages to discover."
        ),
    ) -> None:
        """Serve this farm's tools to a local agent over MCP stdio, as the agent token in
        FARMCORE_TOKEN, or with --http to remote agents, each with its own bearer token. Every
        call is written to the farm's access log."""
        logging.basicConfig(
            stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
        )
        token = os.environ.pop(mcp.TOKEN_ENV, "").strip()
        settings = get_settings()
        if http and not settings.mcp_http_enabled:
            _fail("MCP over HTTP is off: set MCP_HTTP_ENABLED=true to serve it.", 2)
        if not http and not token:
            _fail(f"set {mcp.TOKEN_ENV} to an agent token issued on Sharing.", 2)
        engine = make_engine(settings)
        try:
            ordered = registry.discover(packages=tuple(app_package))
            for manifest in ordered:
                for module in manifest.models:
                    importlib.import_module(module)
            configure_mappers()
            if http:
                _serve_http(make_session_factory(engine), settings, ordered)
                return
            tools = mcp.FarmTools(make_session_factory(engine), settings, ordered)
            try:
                tools.list(token)  # refuse a bad token now, not at the agent's first question
            except mcp.Refused as exc:
                _fail(str(exc))
            except DBAPIError as exc:
                _fail(f"cannot read the database: {database_error(exc)}")
            anyio.run(mcp.serve_stdio, tools, token)
        finally:
            engine.dispose()
