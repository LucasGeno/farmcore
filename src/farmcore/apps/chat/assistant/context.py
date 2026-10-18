"""The `ToolContext` a chat's question is answered with: the chat's farm (the default farm, as for
logs), the owner grant, actor `chat:<id>` and purpose `assistant`, so every tool read is checked
by the repository and written to `access_log` as the assistant's."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from farmcore.core.access import ScopedRepository, default_farm, ensure_owner_grant
from farmcore.core.tools import ToolContext
from farmcore.settings import Settings

PURPOSE = "assistant"


def tool_context(
    session: Session, settings: Settings, chat_id: int, now: datetime, *, lang: str = "en"
) -> ToolContext:
    farm = default_farm(session, settings)
    repo = ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), f"chat:{chat_id}", PURPOSE
    )
    return ToolContext(repo=repo, now=now, tz=ZoneInfo(settings.display_tz), lang=lang)
