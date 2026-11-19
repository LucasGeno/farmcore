"""`farmcore mcp`: serve the farm's read and propose tools to a local agent over MCP stdio.

The agent token comes from `FARMCORE_TOKEN` only (issued on Sharing); the command refuses to
start without a valid one and never prints it. Stdout carries the protocol; logs go to stderr.
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
from farmcore.settings import get_settings


def _fail(message: str, code: int = 1) -> None:
    typer.echo(f"mcp: {message}", err=True)
    raise typer.Exit(code)


def register(cli: typer.Typer) -> None:
    @cli.command(name="mcp")
    def serve(
        app_package: list[str] = typer.Option(
            ["farmcore.apps"], "--app-package", hidden=True, help="App packages to discover."
        ),
    ) -> None:
        """Serve this farm's tools to a local agent over MCP stdio, as the agent token in
        FARMCORE_TOKEN. Every call is written to the farm's access log."""
        logging.basicConfig(
            stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
        )
        token = os.environ.pop(mcp.TOKEN_ENV, "").strip()
        if not token:
            _fail(f"set {mcp.TOKEN_ENV} to an agent token issued on Sharing.", 2)
        settings = get_settings()
        engine = make_engine(settings)
        try:
            ordered = registry.discover(packages=tuple(app_package))
            for manifest in ordered:
                for module in manifest.models:
                    importlib.import_module(module)
            configure_mappers()
            tools = mcp.FarmTools(make_session_factory(engine), settings, ordered, token)
            try:
                tools.list()  # refuse a bad token now, not at the agent's first question
            except mcp.Refused as exc:
                _fail(str(exc))
            except DBAPIError as exc:
                _fail(f"cannot read the database: {database_error(exc)}")
            anyio.run(mcp.serve_stdio, tools)
        finally:
            engine.dispose()
