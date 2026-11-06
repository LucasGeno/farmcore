"""Field observations and irrigation facts through the assistant panel's HTTP boundary."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from farmcore.apps.chat.handlers import ChatDeps
from farmcore.core.models.canonical import Log
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


@pytest.fixture(autouse=True)
def local_parser(client):
    """The panel's chat handlers with the offline parser (tests configure no model)."""
    from farmcore.apps.chat.extractor import RegexExtractor

    app = client.app
    app.state.assistant_chat_deps = ChatDeps(
        app.state.session_factory,
        RegexExtractor(),
        app.state.settings,
        tuple(app.state.manifests),
        weather_reader=app.state.spray_weather_reader,
    )


def _say(client, q, conversation="one"):
    return client.post("/assistant/message", data={"q": q, "conversation": conversation})


def _pending(session) -> list[Log]:
    session.expire_all()
    return list(session.scalars(select(Log).where(Log.status == "pending")).all())


def test_the_panel_previews_then_confirms_an_irrigation(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")

    body = _say(client, "Irrigated Les Granges 20 mm gross at 6").json()

    assert body["kind"] == "draft", body
    assert "irrigation 20 mm gross" in body["text"]
    assert "No spray stock is used and no advice is calculated for irrigation." in body["text"]
    [confirm, cancel] = body["actions"]
    assert confirm["label"].endswith("Confirm") and cancel["label"].endswith("Cancel")
    [pending] = _pending(session)
    assert (pending.type, pending.status) == ("irrigate", "pending")
    assert pending.props == {"irrigation": {"amount": "20", "unit": "mm", "basis": "gross"}}
    assert pending.quantities == []

    done = client.post("/assistant/confirm", data={"action": confirm["action"]}).json()

    assert done["kind"] == "done", done
    assert done["text"].startswith("✅ Logged: P5 Les Granges · irrigation 20 mm gross")
    assert "No spray stock was used and no advice was calculated." in done["text"]
    session.expire_all()
    assert session.get(Log, pending.id).status == "done"


def test_the_panel_asks_for_the_unit_before_any_preview_then_keeps_net(
    client, session, seeded, step
):
    seeded("B")
    step("2024-04-10 06:00")

    first = _say(client, "Irrigated Les Granges at 6").json()

    assert first["kind"] == "clarify", first
    assert first["text"].startswith("How much water"), first["text"]
    assert _pending(session) == []

    body = _say(client, "20 mm net").json()

    assert body["kind"] == "draft", body
    assert "irrigation 20 mm net" in body["text"]
    [pending] = _pending(session)
    assert pending.props == {"irrigation": {"amount": "20", "unit": "mm", "basis": "net"}}
    action = body["actions"][0]["action"]
    assert client.post("/assistant/confirm", data={"action": action}).status_code == 200


def test_the_panel_captures_an_irrigation_said_without_a_verb(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")

    body = _say(client, "P5 20 mm gross at 6").json()

    assert body["kind"] == "draft", body
    [pending] = _pending(session)
    assert (pending.type, pending.parcels[0].alias) == ("irrigate", "P5")
    assert pending.props["irrigation"] == {"amount": "20", "unit": "mm", "basis": "gross"}


def test_the_panel_previews_then_confirms_an_observation(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")

    body = _say(client, "Observed powdery mildew on Les Granges at 6").json()

    assert body["kind"] == "draft", body
    assert "observation: Observed powdery mildew on Les Granges at 6" in body["text"]
    [pending] = _pending(session)
    assert pending.type == "observation"
    assert pending.props == {"observation": {"text": "Observed powdery mildew on Les Granges at 6"}}
    assert pending.quantities == []

    done = client.post("/assistant/confirm", data={"action": body["actions"][0]["action"]})

    assert done.json()["kind"] == "done", done.text
    session.expire_all()
    assert session.get(Log, pending.id).status == "done"


def test_asking_the_panel_never_pretends_to_record_an_irrigation(client, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")

    body = client.get(
        "/assistant/ask", params={"q": "Irrigated P5 20 mm", "conversation": "one"}
    ).json()

    assert body["tool"] is None
    assert "irrigation" in body["text"] and "Telegram" in body["text"]
