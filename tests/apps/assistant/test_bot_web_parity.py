"""N10: the Telegram bot and the web assistant answer "When was P6 last sprayed?" through the
same `core/tools.py` tool (`operations_last_spray`), so they give the same facts."""

import pytest

from farmcore.apps.chat.assistant import fallback
from farmcore.apps.chat.dto import Incoming
from farmcore.apps.chat.extractor import RegexExtractor
from farmcore.apps.chat.handlers import ChatDeps, on_message
from farmcore.core import jobs
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local
from tests.apps.chat.assistant.toolkit import CHAT

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
pytestmark = pytest.mark.data

QUESTION = "When was P6 last sprayed?"


def test_bot_and_web_answer_last_sprayed_through_the_same_tool(
    client, session, settings, session_factory, manifests, seeded, step, monkeypatch
):
    seeded("B")
    step("2024-04-08 06:00")
    step("2024-04-10 06:00")
    confirm_spray(session, settings, "P6", local("2024-04-10 05:30"), "copper-contact")
    run = jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)
    assert run is not None and run.ok
    session.expire_all()

    called: list[str] = []
    real = fallback.call_tool

    def spy(tool, ctx, args):
        called.append(tool.name)
        return real(tool, ctx, args)

    monkeypatch.setattr(fallback, "call_tool", spy)
    deps = ChatDeps(session_factory, RegexExtractor(), settings, tuple(manifests))
    bot = on_message(deps, Incoming(chat_id=CHAT, text=QUESTION, transcript=None, message_id=1))
    web = client.get("/assistant/ask", params={"q": QUESTION}).json()

    assert called == ["operations_last_spray"]  # the bot's tool (the web route reports its own)
    assert web["tool"] == "operations_last_spray"
    assert bot.text.splitlines()[0] == web["text"].splitlines()[0]
    assert "P6 Peyrot was last sprayed on 10 Apr 05:30" in bot.text
