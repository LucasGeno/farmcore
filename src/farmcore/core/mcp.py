"""The farm's tools for an agent over MCP: `farmcore mcp` (stdio) and `--http` (`mcp_http`).

- **Identity.** An agent token (`access.authenticate_token`): from `FARMCORE_TOKEN` on stdio,
  the environment only, or from each HTTP request's `Authorization` header. Never a flag, never
  logged, never in an MCP error or result. Every list and call authenticates the request's
  token afresh, so revoking or expiring the grant refuses the next request of a running server.
  The audit actor is `token:<id>`; the name the MCP client sends about itself is untrusted and
  only logged (stderr) as data.
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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
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
from farmcore.core.drafts import DraftError
from farmcore.core.farm_location import farm_zone
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import AccessLog, Farm
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec, call_tool, collect_tools
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

TOKEN_ENV = "FARMCORE_TOKEN"
LOGGED_CHARS = 64  # characters of a client-sent name (tool, client) kept in a log line
# Longest string argument, as the web assistant's model arguments (a parcel name or alias, a
# day): a local agent has no business sending more, and the tool never needs it.
MAX_ARG_CHARS = 80
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


def _log_refusal(tool: str | None, actor: str, why: str) -> None:
    """A refused request, on stderr only: access_log records data read, and a refusal read
    nothing."""
    logger.warning("mcp refused %r as %s (%s)", (tool or "list")[:LOGGED_CHARS], actor, why)


def _arguments_fit(tool: ToolSpec, args: Any) -> bool:
    """Only the tool's own parameters, matching its JSON Schema, no string over
    `MAX_ARG_CHARS`."""
    return (
        isinstance(args, Mapping)
        and set(args) <= set(tool.param_names)
        and all(len(v) <= MAX_ARG_CHARS for v in args.values() if isinstance(v, str))
        and Draft202012Validator(tool.params).is_valid(dict(args))
    )


def _jsonable(value: Any) -> Any:
    """Facts as plain JSON (dates and decimals as strings)."""
    return json.loads(json.dumps(value, default=str))


class FarmTools:
    """The farm tools of the token each request carries, read live on every request.
    Synchronous: the server runs it in a worker thread."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        manifests: Sequence[Manifest],
        audience: str | None = None,
    ) -> None:
        """`audience`: the resource this server is, for OAuth tokens (None: they are refused)."""
        self._session_factory = session_factory
        self._audience = audience
        self._settings = settings
        self._manifests = list(manifests)

    def _authenticate(
        self, session: Session, token: str, tool: str | None = None
    ) -> ScopedRepository:
        try:
            return authenticate_token(session, token, audience=self._audience)
        except TokenRejected as exc:
            _log_refusal(tool, "a rejected token", "token rejected")
            raise Refused(str(exc)) from None

    def _tools(self, session: Session, repo: ScopedRepository) -> list[ToolSpec]:
        on = registry.live_active(session, self._manifests)
        return offered(collect_tools(on), repo.grant.scopes or ())

    def list(self, token: str) -> list[ToolSpec]:
        with session_scope(self._session_factory) as session:
            return self._tools(session, self._authenticate(session, token))

    def call(
        self, token: str, name: str, args: Mapping[str, Any], client: str | None = None
    ) -> ToolResult:
        """Run tool `name` as `token`; `Refused` when it is not offered, the arguments do not
        match, or it fails."""
        with session_scope(self._session_factory) as session:
            repo = self._authenticate(session, token, name)
            tool = next((t for t in self._tools(session, repo) if t.name == name), None)
            if tool is None:
                _log_refusal(name, repo.actor, "not offered to this token")
                raise Refused(UNAVAILABLE)
            if not _arguments_fit(tool, args):
                _log_refusal(name, repo.actor, "arguments outside the schema")
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
                (client or "")[:LOGGED_CHARS],
            )
            ctx = ToolContext(
                repo=repo,
                now=get_clock(session, self._settings).now(),
                tz=farm_zone(session.get(Farm, repo.farm_id), self._settings),
                manifests=self._manifests,
            )
            try:
                with session.begin_nested():
                    return call_tool(tool, ctx, args)
            except ScopeDenied:
                error = OUT_OF_SCOPE
            except DraftError as exc:
                # A propose tool's plain reason ("not found", "too large", "invalid geometry"):
                # curated words, never internals, so it is safe to show the client.
                error = str(exc)
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


def server(tools: FarmTools, token_of: Callable[[Any], str]) -> Server:
    """The MCP server over `tools`, as the token `token_of(ctx)` gives for each request."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        try:
            specs = await anyio.to_thread.run_sync(tools.list, token_of(ctx))
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
                tools.call, token_of(ctx), params.name, args, _client_name(ctx)
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


async def serve_stdio(tools: FarmTools, token: str) -> None:
    app = server(tools, lambda ctx: token)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())
