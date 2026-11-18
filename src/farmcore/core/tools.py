"""The assistant's tool contract: apps declare tools, chat answers questions with them.

- `ToolSpec`: one tool. `func` is `"module:attr"`, called as `func(ctx, **args) -> ToolResult`.
  `effect` is `"read"` or `"propose"`; `params` is a JSON Schema object (`object_schema`).
  `read` tools never write (there is no `log_spray` tool, on purpose: a spray goes through ✅);
  `propose` tools only write a draft the farmer confirms.
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
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from farmcore.core.manifest import import_string

if TYPE_CHECKING:
    from farmcore.core.access import ScopedRepository


Effect = Literal["read", "propose"]


def object_schema(
    properties: Mapping[str, Any] | None = None, required: Iterable[str] = ()
) -> dict[str, Any]:
    """A JSON Schema object: `properties` maps name to a sub-schema; `required` lists names."""
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties or {})}
    if required:
        schema["required"] = list(required)
    return schema


def vertex_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the Vertex (Gemini) Schema subset from a JSON Schema: type (upper-cased), enum,
    description, required, nested properties and array items. A schema outside that subset
    (no single string `type`, e.g. `anyOf`, `$ref`, a type list) raises `ValueError`."""
    kind = schema.get("type")
    if not isinstance(kind, str):
        raise ValueError(
            f"unsupported schema for Vertex (needs a single string `type`): {dict(schema)!r}"
        )
    out: dict[str, Any] = {"type": kind.upper()}
    for key in ("description", "enum", "required"):
        if key in schema:
            out[key] = schema[key]
    if "properties" in schema:
        out["properties"] = {k: vertex_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = vertex_schema(schema["items"])
    return out


@dataclass(frozen=True)
class ToolSpec:
    name: str  # "operations_last_spray" (underscores: Gemini-safe)
    description: str
    params: dict[str, Any]  # JSON Schema object, e.g. object_schema({"parcel": {"type": "string"}})
    func: str  # "farmcore.apps.operations.tools:last_spray"
    scope: str  # "logs:read"; enforced by the repo, written to access_log
    keywords: tuple[str, ...] = ()  # EN/FR keyword router
    # "read" answers from stored data; "propose" only writes a draft. No default: a tool that
    # forgets to say must not be taken for a read tool.
    effect: Effect = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.effect not in ("read", "propose"):
            raise ValueError(f"tool {self.name!r}: effect must be 'read' or 'propose'")

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(self.params.get("properties", {}))


@dataclass(frozen=True)
class ToolContext:
    repo: ScopedRepository  # actor chat:<id>, purpose "assistant", the chat's farm
    now: datetime  # shared clock, replay-aware
    tz: ZoneInfo
    lang: str = "en"  # language of ToolResult.text ("en" or "fr")
    # The viewer's active role on this farm; None for anonymous, grant-only and bot contexts.
    # A tool that reads member-only material must check it, never assume the repo's actor.
    member_role: str | None = None
    is_admin: bool = False


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
            vertex_schema(tool.params)  # refuse a schema Vertex cannot take, once, at load
            tools.append(tool)
    return tools


def call_tool(spec: ToolSpec, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    """Run one tool: `func(ctx, **args)`."""
    return import_string(spec.func)(ctx, **dict(args))
