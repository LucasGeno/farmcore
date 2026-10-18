"""Demo questions and the web spray log, through the assistant's HTTP boundary."""

import pytest
from sqlalchemy import select

from farmcore.core.models.canonical import Log
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


@pytest.fixture(autouse=True)
def local_parser(client):
    """The web panel's chat handlers with the offline parser (tests configure no model)."""
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.apps.chat.handlers import ChatDeps

    app = client.app
    app.state.assistant_chat_deps = ChatDeps(
        app.state.session_factory,
        RegexExtractor(),
        app.state.settings,
        tuple(app.state.manifests),
        weather_reader=app.state.spray_weather_reader,
    )


def _ask(client, q, conversation="one"):
    return client.get("/assistant/ask", params={"q": q, "conversation": conversation}).json()


def _say(client, q, conversation="one"):
    return client.post("/assistant/message", data={"q": q, "conversation": conversation})


def test_which_parcels_did_i_spray_names_only_the_sprayed_parcels(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    for alias in ("P5", "P6"):
        scenes.confirm_spray(
            session,
            client.app.state.settings,
            alias,
            scenes.local("2024-04-09 07:00"),
            "copper-contact",
        )
    body = _ask(client, "Which parcels did I spray?")
    assert body["tool"] == "operations_recent_sprays"
    assert "P5 Les Granges" in body["text"] and "P6 Peyrot" in body["text"]
    assert "P7" not in body["text"]


def test_did_i_spray_a_parcel_reads_its_last_spray(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    scenes.confirm_spray(
        session,
        client.app.state.settings,
        "P6",
        scenes.local("2024-04-09 07:00"),
        "copper-contact",
    )
    for q in ("Did I spray P6?", "When did I last spray P6?"):
        body = _ask(client, q, conversation=q)
        assert body["tool"] == "operations_last_spray", q
        assert "P6 Peyrot" in body["text"] and "09 Apr" in body["text"], body["text"]


def test_pronoun_follow_up_keeps_the_last_named_parcel_across_a_parcelless_turn(
    client, seeded, step
):
    seeded("B")
    step("2024-04-10 06:00")
    _ask(client, "When was P5 last sprayed?")
    assert _ask(client, "Which days this week are good?")["tool"] == "assistant_plan"
    body = _ask(client, "Is it protected?")
    assert body["tool"] == "advisor_parcel_status"
    assert body["facts"]["parcel"] == "P5 Les Granges"


def test_web_spray_log_asks_only_for_missing_fields_then_confirms(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    first = _say(client, "I sprayed Les Granges with copper at 6")
    assert first.status_code == 200
    body = first.json()
    assert body["kind"] == "clarify"
    assert body["text"].startswith("How much"), body["text"]
    assert not session.scalars(select(Log).where(Log.status == "pending")).all()

    body = _say(client, "2 kg per hectare").json()
    assert body["kind"] == "draft", body
    assert "P5 Les Granges" in body["text"] and "2 kg/ha" in body["text"]
    assert "Checks:" in body["text"]
    [confirm, cancel] = body["actions"]
    assert confirm["label"].endswith("Confirm") and cancel["label"].endswith("Cancel")
    [pending] = session.scalars(select(Log).where(Log.status == "pending")).all()
    assert pending.sources[0].external_key.startswith("web:")

    done = client.post("/assistant/confirm", data={"action": confirm["action"]}).json()
    assert done["text"].startswith("✅ Logged"), done["text"]
    session.expire_all()
    assert session.get(Log, pending.id).status == "done"
    # The read tools see the new record at once, in the same panel conversation.
    after = _say(client, "Which parcels did I spray?").json()
    assert "P5 Les Granges" in after["text"]
    assert after["fresh_conversation"] is False


def test_web_message_still_answers_questions_and_cancel_clears_the_draft(
    client, session, seeded, step
):
    seeded("B")
    step("2024-04-10 06:00")
    assert (
        _say(client, "I sprayed copper 2 kg this morning").json()["text"].startswith("Which parcel")
    )
    question = _say(client, "When was P5 last sprayed?").json()
    assert question["tool"] == "operations_last_spray"
    # The open clarification survives a question and is then cancelled.
    assert _say(client, "cancel").json()["text"].startswith("❌ Cancelled")
    assert _say(client, "P5").json().get("kind") != "draft"
    assert not session.scalars(select(Log).where(Log.status == "pending")).all()


def test_only_the_signed_in_farmer_can_log_from_the_web(client, session, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    client.cookies.set("farmcore_tour", "1")
    assert _say(client, "I sprayed P5 with copper 2 kg at 6").status_code == 403
    body = _ask(client, "I sprayed P5 with copper 2 kg at 6")
    assert body["tool"] is None
    assert "Telegram" in body["text"]
    assert not session.scalars(select(Log).where(Log.status == "pending")).all()
    client.cookies.clear()
    action = _say(client, "P5 copper 2 kg at 6").json()["actions"][0]["action"]
    client.cookies.set("farmcore_tour", "1")
    assert client.post("/assistant/confirm", data={"action": action}).status_code == 403


def test_web_confirm_refuses_a_telegram_chats_draft(client, session, seeded, step):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.apps.chat.handlers import ChatDeps, on_message
    from tests.apps.chat.test_handlers import voice

    seeded("B")
    step("2024-04-10 06:00")
    deps = ChatDeps(client.app.state.session_factory, RegexExtractor(), client.app.state.settings)
    telegram = on_message(deps, voice("P5 copper 2 kg at 6", 900))
    action = telegram.buttons[0][1]
    body = client.post("/assistant/confirm", data={"action": action}).json()
    assert "no longer exists" in body["text"]
    assert session.scalars(select(Log).where(Log.status == "pending")).all()


def test_no_model_farm_questions_never_reach_the_model(client, session, seeded, step):
    from farmcore.apps.assistant.llm import Choice
    from farmcore.core.models.canonical import Farm
    from tests.apps.assistant.test_llm import FakeChooser

    seeded("B")
    step("2024-04-10 06:00")
    from farmcore.core.access import ensure_owner_grant

    es = Farm(name="Restricted farm", props={"no_model": True})
    session.add(es)
    session.flush()
    ensure_owner_grant(session, es)
    session.commit()
    fake = FakeChooser(Choice("operations_last_spray"))
    client.app.state.assistant_chooser = fake
    body = client.get(
        "/assistant/ask", params={"q": "When was P5 last sprayed?", "farm": es.id}
    ).json()
    assert fake.calls == [] and body["via"] == "keywords"
    _ask(client, "When was P5 last sprayed?")
    assert len(fake.calls) == 1  # the demo farm still uses the model
