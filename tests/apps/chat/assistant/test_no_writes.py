"""Every collected tool, run on the seeded Scene B farm: no log, advice, event or log_source row
is written, and each call writes at least one access_log row with purpose `assistant`."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from farmcore.core.models.canonical import AccessLog
from farmcore.core.tools import call_tool, collect_tools
from tests.apps.advisor import scenes
from tests.apps.chat.assistant.toolkit import ctx_at, write_counts

pytestmark = pytest.mark.data

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

ARGS = {"parcel": "Peyrot", "day": "tomorrow"}


def assistant_rows(session) -> int:
    session.flush()
    return session.scalar(
        select(func.count()).select_from(AccessLog).where(AccessLog.purpose == "assistant")
    )


def test_every_tool_is_read_only_and_audited(session, settings, manifests, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    tools = collect_tools(manifests)
    assert len(tools) == 6
    before = write_counts(session)
    ctx = ctx_at(session, settings, "2024-04-10 06:00")

    for tool in tools:
        rows = assistant_rows(session)
        args = {k: v for k, v in ARGS.items() if k in tool.params}
        result = call_tool(tool, ctx, args)
        assert "error" not in result.facts, tool.name
        assert assistant_rows(session) > rows, tool.name

    assert write_counts(session) == before
