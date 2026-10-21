"""Assistant requests on real, separately committed connections (the deployed shape).

The shared-connection `client` fixture runs every session on one connection, so it cannot see
two request-time sessions blocking each other; this file uses `pooled_session_factory`. It runs
in the default loop: the hang it guards took the whole installation down.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from farmcore.apps.chat.transcribe import Transcript
from farmcore.core.models.canonical import Farm
from farmcore.main import create_app
from tests.apps.chat.test_handlers import COPPER, seed_farm


def test_signed_in_spray_message_and_confirm_answer_instead_of_hanging(
    settings, pooled_session_factory
):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.apps.chat.handlers import ChatDeps

    pooled = pooled_session_factory
    with pooled() as session:
        seed_farm(session, settings)

    app = create_app(settings, session_factory=pooled)
    app.state.assistant_chat_deps = ChatDeps(
        pooled, RegexExtractor(), settings, tuple(app.state.manifests)
    )
    token = settings.admin_token.get_secret_value()
    with TestClient(app, headers={"X-Admin-Token": token}) as client:
        draft = client.post(
            "/assistant/message", data={"q": f"sprayed P1 with {COPPER} 2 kg/ha this morning"}
        ).json()
        assert draft["kind"] == "draft", draft
        done = client.post("/assistant/confirm", data={"action": draft["actions"][0]["action"]})
    assert done.json()["text"].startswith("✅ Logged"), done.json()


class Slow:
    """Stands in for the speech model or Gemini: blocks until the test has written."""

    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()

    def _wait(self):
        self.started.set()
        self.release.wait(10)

    def transcribe(self, path, hints):
        self._wait()
        return Transcript("When was P1 last sprayed?", "en", 3.0)

    def choose(self, question, tools):
        self._wait()
        return None  # the keyword router answers


SLOW_CALLS = {
    "transcription": lambda c: c.post(
        "/assistant/transcribe", files={"audio": ("q.webm", b"OggS" + b"\0" * 200, "audio/webm")}
    ),
    "model tool choice": lambda c: c.post(
        "/assistant/message", data={"q": "When was P1 last sprayed?"}
    ),
}


@pytest.mark.parametrize("call", SLOW_CALLS)
def test_farm_writes_go_through_while_the_assistant_waits_on_a_slow_external_call(
    settings, pooled_session_factory, call
):
    """A 60 s transcription (or a model call) must not hold the farm's write locks."""
    pooled = pooled_session_factory
    with pooled() as session:
        seed_farm(session, settings)
        farm_id = session.query(Farm.id).scalar()

    slow = Slow()
    app = create_app(settings, session_factory=pooled)
    app.state.assistant_transcriber = app.state.assistant_chooser = slow
    token = settings.admin_token.get_secret_value()
    with (
        TestClient(app, headers={"X-Admin-Token": token}) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(SLOW_CALLS[call], client)
        try:
            assert slow.started.wait(10), pending.result()
            write = client.post(
                "/operations/production", data={"mode": "organic"}, follow_redirects=False
            )
        finally:
            slow.release.set()
        assert write.status_code == 303
        assert pending.result().status_code == 200
    with pooled() as session:
        assert session.get(Farm, farm_id).props["production"] == "organic"
