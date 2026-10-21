"""The web spray capture on real, separately committed connections (the deployed shape).

The shared-connection `client` fixture runs every session on one connection, so it cannot see
two request-time sessions blocking each other; this file uses a pooled engine like production.
It runs in the default loop although seeding on real commits takes about 3 s: the hang it guards
took the whole installation down.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import registry
from farmcore.core import seed as core_seed
from farmcore.core.commands.seed import run_seeds


@pytest.fixture
def pooled(settings, committed_session_factory) -> Iterator[sessionmaker[Session]]:
    """Real commits on a pooled engine whose lock waits give up after 1 s instead of hanging."""
    engine = create_engine(settings.database_url, connect_args={"options": "-c lock_timeout=1000"})
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_signed_in_spray_message_and_confirm_answer_instead_of_hanging(
    settings, pooled, monkeypatch, data_dir
):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.apps.chat.handlers import ChatDeps
    from farmcore.main import create_app

    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    monkeypatch.setattr(ops_seed, "get_settings", lambda: settings)
    with pooled() as session:
        ordered = registry.discover()
        manifests = registry.active_manifests(ordered, registry.read_states(session, ordered))
        run_seeds(session, manifests, scene=None, data_dir=data_dir)
        session.commit()

    app = create_app(settings, session_factory=pooled)
    app.state.assistant_chat_deps = ChatDeps(
        pooled, RegexExtractor(), settings, tuple(app.state.manifests)
    )
    token = settings.admin_token.get_secret_value()
    with TestClient(app, headers={"X-Admin-Token": token}) as client:
        draft = client.post(
            "/assistant/message",
            data={"q": "sprayed P5 with bouillie bordelaise 2 kg/ha this morning"},
        ).json()
        assert draft["kind"] == "draft", draft
        done = client.post("/assistant/confirm", data={"action": draft["actions"][0]["action"]})
    assert done.json()["text"].startswith("✅ Logged"), done.json()
