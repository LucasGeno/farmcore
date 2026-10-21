"""The web spray capture on real, separately committed connections (the deployed shape).

The shared-connection `client` fixture runs every session on one connection, so it cannot see
two request-time sessions blocking each other; this file uses a pooled engine like production.
It runs in the default loop: the hang it guards took the whole installation down.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def pooled(settings, committed_session_factory) -> Iterator[sessionmaker[Session]]:
    """Real commits on a pooled engine whose lock waits give up after 1 s instead of hanging."""
    engine = create_engine(settings.database_url, connect_args={"options": "-c lock_timeout=1000"})
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_signed_in_spray_message_and_confirm_answer_instead_of_hanging(settings, pooled):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.apps.chat.handlers import ChatDeps
    from farmcore.main import create_app
    from tests.apps.chat.test_handlers import COPPER, seed_farm

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
