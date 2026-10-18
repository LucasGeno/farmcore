"""Helpers for the assistant tool tests (not a test module): a chat's `ToolContext` at a local
time, the assistant's access_log rows, and the row counts a read-only tool must leave alone."""

from __future__ import annotations

from sqlalchemy import func, select

from farmcore.apps.chat.assistant.context import PURPOSE, tool_context
from farmcore.core.models.canonical import AccessLog, Advice, Event, Log, LogSource
from tests.apps.advisor.scenes import local

CHAT = 4242  # synthetic Telegram chat id


def ctx_at(session, settings, when: str, lang: str = "en"):
    """The chat's tool context with the clock at local `when` ("2024-04-10 06:00")."""
    return tool_context(session, settings, CHAT, local(when), lang=lang)


def assistant_reads(session) -> list[tuple[str, str, str]]:
    """(actor, resource, scope) of every access_log row with purpose `assistant`."""
    session.flush()
    rows = session.execute(
        select(AccessLog.actor, AccessLog.resource, AccessLog.scope)
        .where(AccessLog.purpose == PURPOSE)
        .order_by(AccessLog.id)
    )
    return [tuple(r) for r in rows]


def write_counts(session) -> dict[str, int]:
    """Rows a tool must never add: log, advice, event, log_source."""
    session.flush()
    return {
        m.__tablename__: session.scalar(select(func.count()).select_from(m))
        for m in (Log, Advice, Event, LogSource)
    }


def keys(facts) -> set[str]:
    """Every key in `facts`, nested dicts and lists of dicts included."""
    out: set[str] = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                out.add(k)
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)

    walk(facts)
    return out
