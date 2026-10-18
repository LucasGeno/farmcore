"""Chat assistant: a question in the chat is answered from the apps' read-only tools.

`handlers._on_message` sends a *question* (`router.intent`) here, after the bot's allowlist gate;
logs and everything else keep the extractor → pending → ✅/❌ path. The tools come from the
visible apps (`visible_from_states` on the live `app_module` state, then `collect_tools`), read
through the chat's repository with purpose `assistant` (`context.tool_context`), and the keyword
fallback picks one and replies with its text and "Open card" buttons. Nothing leaves the host;
nothing is written but `access_log`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from farmcore.apps.chat.assistant import fallback, router
from farmcore.apps.chat.assistant.context import tool_context
from farmcore.apps.chat.dto import Outgoing
from farmcore.core.registry import read_states, visible_from_states
from farmcore.core.tools import collect_tools
from farmcore.db import session_scope

if TYPE_CHECKING:
    from farmcore.apps.chat.handlers import ChatDeps


def answer(deps: ChatDeps, chat_id: int, text: str) -> Outgoing:
    """The reply to one question from chat `chat_id` (raises `FarmNotSeeded` before the seed)."""
    lang = router.language(text)
    with session_scope(deps.session_factory) as session:
        # Live state: an app switched off on /apps loses its tools without a bot restart.
        manifests = list(deps.manifests)
        tools = collect_tools(visible_from_states(manifests, read_states(session, manifests)))
        now = deps.clock(session, deps.settings).now()
        ctx = tool_context(session, deps.settings, chat_id, now, lang=lang)
        return fallback.answer(text, tools, ctx, deps.settings.public_base_url)
