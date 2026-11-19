"""The farm's tools for a local agent over MCP (`farmcore mcp`, stdio).

- **Identity.** An agent token (`access.authenticate_token`) from `FARMCORE_TOKEN`, the
  environment only: never a flag, never logged, never in an MCP error or result. Every list and
  call authenticates it afresh, so revoking or expiring the grant refuses the next request of a
  running server. The audit actor is `token:<id>`; the name the MCP client sends about itself is
  untrusted and only logged (stderr) as data.
- **Tools** (`offered`): the tools of the apps that are on now (`registry.live_active`, read on
  every request, so a switched-off app's tools vanish from the list and are refused at call
  time without a restart), limited to the token's scopes. A `propose` tool also needs
  `drafts:propose`, checked here explicitly: the repository's check does not cover drafts.
  Hints: `read` is read-only; `propose` is a non-destructive write.
- **Calls.** Arguments must match the tool's JSON Schema, with no other keys, before the tool
  runs. Each call writes one `access_log` row (`resource` `mcp:<tool>`, the tool's scope), next
  to the rows of the reads the tool makes; a tool that fails rolls back its own writes, keeps
  that row and answers with a generic error. The farm is the token's: no argument names one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

import anyio
import mcp_types as types
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from sqlalchemy.orm import Session, sessionmaker

import farmcore
from farmcore.core import registry
from farmcore.core.access import (
    DRAFTS_PROPOSE,
    ScopeDenied,
    ScopedRepository,
    TokenRejected,
    authenticate_token,
)
from farmcore.core.clock import get_clock
from farmcore.core.farm_location import farm_zone
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import AccessLog, Farm
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec, call_tool, collect_tools
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

TOKEN_ENV = "FARMCORE_TOKEN"
CLIENT_NAME_LOGGED = 64  # characters of the client's self-reported name kept in the log
UNAVAILABLE = "This tool is not available to this token."
BAD_ARGUMENTS = "The arguments do not match the tool's input schema."
OUT_OF_SCOPE = "The tool needs data this token may not read."
FAILED = "The tool failed."


class Refused(Exception):
    """A request answered with an error; `str()` is safe to show the client."""


def offered(tools: Iterable[ToolSpec], scopes: Collection[str]) -> list[ToolSpec]:
    """The tools a token holding `scopes` may see and call: those whose scope it holds, and a
    `propose` tool only when it also holds `drafts:propose`."""
    held = set(scopes)
    return [t for t in tools if t.scope in held and (t.effect == "read" or DRAFTS_PROPOSE in held)]


def _jsonable(value: Any) -> Any:
    """Facts as plain JSON (dates and decimals as strings)."""
    return json.loads(json.dumps(value, default=str))


class FarmTools:
    """The token's farm tools, read live on every request. Synchronous: the server runs it in a
    worker thread."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        manifests: Sequence[Manifest],
        token: str,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._manifests = list(manifests)
        self._token = token

    def _authenticate(self, session: Session) -> ScopedRepository:
        try:
            return authenticate_token(session, self._token)
        except TokenRejected as exc:
            raise Refused(str(exc)) from None

    def _tools(self, session: Session, repo: ScopedRepository) -> list[ToolSpec]:
        on = registry.live_active(session, self._manifests)
        return offered(collect_tools(on), repo.grant.scopes or ())

    def list(self) -> list[ToolSpec]:
        with session_scope(self._session_factory) as session:
            return self._tools(session, self._authenticate(session))

    def call(self, name: str, args: Mapping[str, Any], client: str | None = None) -> ToolResult:
        """Run tool `name` as the token; `Refused` when it is not offered, the arguments do not
        match, or it fails."""
        with session_scope(self._session_factory) as session:
            repo = self._authenticate(session)
            tool = next((t for t in self._tools(session, repo) if t.name == name), None)
            if tool is None:
                raise Refused(UNAVAILABLE)
            if not isinstance(args, Mapping) or not set(args) <= set(tool.param_names):
                raise Refused(BAD_ARGUMENTS)
            if not Draft202012Validator(tool.params).is_valid(dict(args)):
                raise Refused(BAD_ARGUMENTS)
            session.add(
                AccessLog(
                    actor=repo.actor,
                    farm_id=repo.farm_id,
                    grant_id=repo.grant.id,
                    resource=f"mcp:{tool.name}",
                    scope=tool.scope,
                    purpose=repo.purpose,
                )
            )
            session.flush()
            logger.info(
                "mcp call %s as %s (client says %r)",
                tool.name,
                repo.actor,
                (client or "")[:CLIENT_NAME_LOGGED],
            )
            ctx = ToolContext(
                repo=repo,
                now=get_clock(session, self._settings).now(),
                tz=farm_zone(session.get(Farm, repo.farm_id), self._settings),
            )
            try:
                with session.begin_nested():
                    return call_tool(tool, ctx, args)
            except ScopeDenied:
                error = OUT_OF_SCOPE
            except Exception as exc:
                logger.warning("mcp tool %s failed (%s)", tool.name, type(exc).__name__)
                error = FAILED
        raise Refused(error)  # after the commit: the call's audit row stays


def _annotations(tool: ToolSpec) -> types.ToolAnnotations:
    if tool.effect == "read":
        return types.ToolAnnotations(read_only_hint=True)
    return types.ToolAnnotations(read_only_hint=False, destructive_hint=False)


def _client_name(ctx: Any) -> str | None:
    params = ctx.session.client_params
    return params.client_info.name if params is not None else None


def server(tools: FarmTools) -> Server:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        try:
            specs = await anyio.to_thread.run_sync(tools.list)
        except Refused as exc:
            raise MCPError(types.INVALID_REQUEST, str(exc)) from None
        except Exception as exc:
            logger.warning("mcp list failed (%s)", type(exc).__name__)
            raise MCPError(types.INTERNAL_ERROR, "Listing tools failed.") from None
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t.name,
                    description=t.description,
                    input_schema=t.params,
                    annotations=_annotations(t),
                )
                for t in specs
            ]
        )

    async def call(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        args = params.arguments or {}
        try:
            result = await anyio.to_thread.run_sync(
                tools.call, params.name, args, _client_name(ctx)
            )
        except Refused as exc:
            message = str(exc)
        except Exception as exc:
            logger.warning("mcp call failed (%s)", type(exc).__name__)
            message = FAILED
        else:
            links = [{"label": label, "path": path} for label, path in result.links]
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=result.text)],
                structured_content={"facts": _jsonable(result.facts), "links": links},
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)], is_error=True
        )

    return Server(
        "farmcore", version=farmcore.__version__, on_list_tools=list_tools, on_call_tool=call
    )


async def serve_stdio(tools: FarmTools) -> None:
    app = server(tools)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())
