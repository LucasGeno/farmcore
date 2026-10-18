"""Conversation behavior through the existing HTTP assistant boundary."""

import pytest

from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


def test_followup_reuses_intent_but_reads_the_new_parcel(client, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    first = client.get(
        "/assistant/ask", params={"q": "When was P5 last sprayed?", "conversation": "one"}
    )
    assert first.json()["tool"] == "operations_last_spray"
    second = client.get("/assistant/ask", params={"q": "And P6?", "conversation": "one"}).json()
    assert second["tool"] == "operations_last_spray"
    assert "P6" in second["text"] and "P5" not in second["text"]
    assert (
        client.get("/assistant/ask", params={"q": "And P7?", "conversation": "new"}).json()["tool"]
        is None
    )


def test_conversations_expire_on_idle_clock_and_viewer_change(client, seeded, step, monkeypatch):
    from farmcore.apps.assistant import conversations

    seeded("B")
    step("2024-04-10 06:00")
    wall = [10.0]
    monkeypatch.setattr(conversations, "monotonic", lambda: wall[0])

    def ask(q):
        return client.get("/assistant/ask", params={"q": q, "conversation": "one"}).json()

    ask("When was P5 last sprayed?")
    wall[0] += 1800
    assert ask("And P6?")["tool"] is None
    ask("When was P5 last sprayed?")
    step("2024-04-11 06:00")
    assert ask("And P6?")["tool"] is None
    ask("When was P5 last sprayed?")
    client.cookies.set("farmcore_tour", "1")
    assert ask("And P6?")["tool"] is None


def test_context_is_bounded_and_excludes_answers_and_other_browsers(client, seeded, step):
    import json

    from farmcore.apps.assistant.llm import Choice
    from tests.apps.assistant.test_llm import FakeChooser

    seeded("B")
    step("2024-04-10 06:00")
    fake = FakeChooser(Choice("operations_last_spray"))
    client.app.state.assistant_chooser = fake
    for p in ("P1", "P2", "P3", "P4", "P5"):
        body = client.get(
            "/assistant/ask", params={"q": f"When was {p} last sprayed?", "conversation": "one"}
        ).json()
    context = json.loads(fake.calls[-1][0])
    assert [t["parcel"] for t in context["previous_turns"]] == ["P2", "P3", "P4"]
    assert context["current_question"] == "When was P5 last sprayed?"
    assert all(set(t) == {"question", "selected_tool", "parcel"} for t in context["previous_turns"])
    assert body["text"] not in fake.calls[-1][0]
    client.cookies.clear()
    client.get("/assistant/ask", params={"q": "And P6?", "conversation": "one"})
    assert fake.calls[-1][0] == "And P6?"


@pytest.mark.spanish_data
def test_followups_never_cross_farms_or_override_explicit_parcels(client, session, seeded, step):
    from sqlalchemy import select

    from farmcore.apps.assistant.llm import Choice
    from farmcore.core.models.canonical import Farm
    from tests.apps.assistant.test_llm import FakeChooser

    seeded("B")
    step("2024-04-10 06:00")
    params = {"conversation": "one"}
    client.get("/assistant/ask", params={**params, "q": "When was P5 last sprayed?"})
    client.app.state.assistant_chooser = FakeChooser(
        Choice("operations_last_spray", {"parcel": "P5"})
    )
    body = client.get("/assistant/ask", params={**params, "q": "And P6?"}).json()
    assert "P6" in body["text"] and "P5" not in body["text"]
    del client.app.state.assistant_chooser
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    body = client.get("/assistant/ask", params={**params, "q": "And P6?", "farm": es.id}).json()
    assert body["fresh_conversation"] and body["tool"] is None


@pytest.mark.parametrize("question", ["And P99?", "And Unknown Vineyard?"])
def test_unknown_parcel_followup_never_reuses_the_previous_parcel(client, seeded, step, question):
    seeded("B")
    step("2024-04-10 06:00")
    client.get("/assistant/ask", params={"q": "When was P5 last sprayed?", "conversation": "one"})
    body = client.get("/assistant/ask", params={"q": question, "conversation": "one"}).json()
    assert body["facts"].get("error") == "unknown parcel"
    assert "I don't know that parcel" in body["text"]


def test_farm_advice_followup_narrows_to_the_named_parcel(client, seeded, step):
    seeded("B")
    step("2024-04-10 06:00")
    first = client.get(
        "/assistant/ask", params={"q": "Do I need to spray anything?", "conversation": "one"}
    ).json()
    assert first["tool"] == "advisor_today"
    body = client.get("/assistant/ask", params={"q": "And P6?", "conversation": "one"}).json()
    assert body["tool"] == "advisor_parcel_status"
    assert body["facts"]["parcel"] == "P6 Peyrot"
