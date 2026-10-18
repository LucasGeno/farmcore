"""The assistant's tool contract: apps declare read-only tools, chat answers questions with them.

- `ToolSpec`: one tool. `func` is `"module:attr"`, called as `func(ctx, **args) -> ToolResult`.
  Tools never write (there is no `log_spray` tool, on purpose: a spray goes through ✅).
- `ToolContext`: what a tool reads through: a `ScopedRepository` (actor `chat:<id>`, purpose
  `assistant`, the chat's farm), so every read checks the grant and writes `access_log`; the
  shared clock's `now` (replay-aware: every read is bounded by it); the display time zone; and
  the language the plain-words `text` is written in.
- `ToolResult`: `facts` holds allowlisted keys only (sent to the model), `text` the
  plain-words answer (the no-LLM fallback sends it), `links` ("Open card P5", "/advisor/...").
- `collect_tools(manifests)`: the active apps' tools in load order, from each manifest's
  `tools=` (the registry's manifests: an uninstalled app's tools disappear). The core imports no
  app code: an app that declares no `tools=` brings none.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from farmcore.core.manifest import import_string

if TYPE_CHECKING:
    from farmcore.core.access import ScopedRepository


@dataclass(frozen=True)
class ToolSpec:
    name: str  # "operations_last_spray" (underscores: Gemini-safe)
    description: str
    params: dict[str, Any]  # Vertex Schema subset, e.g. {"parcel": "STRING"}
    func: str  # "farmcore.apps.operations.tools:last_spray"
    scope: str  # "logs:read"; enforced by the repo, written to access_log
    keywords: tuple[str, ...] = ()  # EN/FR keyword router


@dataclass(frozen=True)
class ToolContext:
    repo: ScopedRepository  # actor chat:<id>, purpose "assistant", the chat's farm
    now: datetime  # shared clock, replay-aware
    tz: ZoneInfo
    lang: str = "en"  # language of ToolResult.text ("en" or "fr")


@dataclass(frozen=True)
class ToolResult:
    facts: dict[str, Any]  # allowlisted keys only (sent to the model)
    text: str  # plain-words rendering
    links: tuple[tuple[str, str], ...] = ()  # ("Open card P5", "/advisor/parcels/5")


def declared_tools(manifest: Any) -> tuple[ToolSpec, ...]:
    """The manifest's `tools`; `getattr` so a test stub without the field brings none."""
    return tuple(getattr(manifest, "tools", ()) or ())


def collect_tools(manifests: Iterable[Any]) -> list[ToolSpec]:
    """Every active manifest's tools, in load order. Tool names must be unique."""
    tools: list[ToolSpec] = []
    seen: dict[str, str] = {}
    for m in manifests:
        for tool in declared_tools(m):
            if tool.name in seen:
                raise ValueError(
                    f"tool {tool.name!r} is declared by both {seen[tool.name]!r} and {m.name!r}"
                )
            seen[tool.name] = m.name
            tools.append(tool)
    return tools


def call_tool(spec: ToolSpec, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    """Run one tool: `func(ctx, **args)`."""
    return import_string(spec.func)(ctx, **dict(args))
