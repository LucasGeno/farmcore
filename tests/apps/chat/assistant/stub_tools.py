"""A stub tool imported by dotted path in test_collect_tools.py (not a test module)."""

from __future__ import annotations

from typing import Any

from farmcore.core.tools import ToolResult


def echo(ctx: Any, parcel: str = "") -> ToolResult:
    return ToolResult(facts={"parcel": parcel}, text=f"{ctx.marker} {parcel}")
