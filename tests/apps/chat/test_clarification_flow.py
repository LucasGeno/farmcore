"""Farmer-visible continuation, isolated from Telegram and model network calls."""

import itertools
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from farmcore.apps.chat.dto import Callback
from farmcore.apps.chat.extractor import RegexExtractor
from farmcore.apps.chat.handlers import on_callback, on_message
from farmcore.core.models.canonical import Log, Product
from tests.apps.chat.test_handlers import CHAT, COPPER, deps, seed_farm, voice


def test_clarification_keeps_parcel_dose_and_time_across_recreated_dependencies(
    session_factory, session, settings
):
    seed_farm(session, settings)

    def send(text, message_id):
        # Recreate dependencies: history must survive a bot restart, rather than live in a dict.
        return on_message(
            deps(session_factory, settings, RegexExtractor()), voice(text, message_id)
        )

    first = send("I sprayed this morning, 2 kg per hectare", 101)
    assert first.text.startswith("Which parcel")
    second = send("P5", 102)
    assert second.text.startswith("Which product")
    third = send("copper", 103)

    assert third.buttons, third.text
    assert "P5 Les Granges" in third.text
    assert "2 kg/ha" in third.text
    [log] = list(session.scalars(select(Log)))
    assert log.status == "pending"
    assert [p.alias for p in log.parcels] == ["P5"]
    assert [(q.label, float(q.value), q.unit) for q in log.quantities] == [(COPPER, 2, "kg/ha")]

    on_callback(
        deps(session_factory, settings, RegexExtractor()),
        Callback(chat_id=CHAT, data=third.buttons[0][1]),
    )
    # The next operation must not inherit P5 or the previous dose.
    fresh = send("I sprayed copper", 104)
    assert fresh.text.startswith("Which parcel")


def test_separate_rates_survive_a_missing_parcel_clarification(session_factory, session, settings):
    from tests.apps.chat.test_handlers import SULPHUR

    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    first = on_message(d, voice("copper 2 kg/ha and sulphur 1 kg/ha at 6", 501))
    assert first.text.startswith("Which parcel")
    reply = on_message(d, voice("P5", 502))

    assert reply.buttons, reply.text
    [log] = list(session.scalars(select(Log)))
    assert sorted((q.label, float(q.value)) for q in log.quantities) == [(COPPER, 2), (SULPHUR, 1)]


def test_product_rate_and_label_warning_are_visible_before_confirmation(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    session.get(Product, ids[COPPER]).props = {"label_max_dose_kg_ha": "1"}
    session.commit()
    d = deps(session_factory, settings, RegexExtractor())

    reply = on_message(d, voice("P5 copper 2 kg/ha at 6", 201))

    assert f"{COPPER}: 2 kg/ha" in reply.text
    assert "Warning: Copper dose 2 kg/ha, above the label max 1" in reply.text
    [log] = list(session.scalars(select(Log)))
    assert log.status == "pending"
    assert reply.buttons[0][0] == "✅ Confirm"

    result = on_callback(d, Callback(chat_id=CHAT, data=reply.buttons[0][1]))
    assert "Warning: Copper dose 2 kg/ha, above the label max 1" in result.text


def test_two_product_rates_are_kept_separate_in_preview_and_confirmed_record(
    session_factory, session, settings
):
    seed_farm(session, settings)
    session.add(
        Product(
            name="PAJO",
            authorisation_no="TEST-3",
            active_substance="cymoxanil",
            product_type="penetrant",
            targets=["downy"],
            props={"label_max_dose_kg_ha": "0.45"},
        )
    )
    session.commit()
    d = deps(session_factory, settings, RegexExtractor())

    reply = on_message(d, voice("P5 copper 2 kg/ha and PAJO 0.3 kg/ha at 6", 301))

    assert "PAJO: 0.3 kg/ha" in reply.text
    assert "OK: Cymoxanil dose 0.3 kg/ha, label max 0.45" in reply.text
    assert "Warning: Cymoxanil dose 2" not in reply.text
    on_callback(d, Callback(chat_id=CHAT, data=reply.buttons[0][1]))
    [log] = list(session.scalars(select(Log)))
    assert sorted((q.label, float(q.value), q.unit) for q in log.quantities) == [
        (COPPER, 2, "kg/ha"),
        ("PAJO", 0.3, "kg/ha"),
    ]


def test_cancel_clears_unfinished_context_and_outstanding_confirmation_buttons(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    echo = on_message(d, voice("P5 copper 2 kg/ha at 6", 401))
    on_message(d, voice("P6 3 kg/ha", 402))

    reply = on_message(d, voice("/cancel", 403))

    assert reply.text == "❌ Cancelled. Nothing was logged."
    stale = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))
    assert stale.text == "This spray was cancelled or no longer exists."
    assert list(session.scalars(select(Log))) == []
    fresh = on_message(d, voice("copper", 404))
    assert fresh.text.startswith("Which parcel")


def test_reset_during_extraction_does_not_restore_old_input(session_factory, session, settings):
    import httpx

    from farmcore.core.models.kernel import ClockState
    from tests.apps.chat.test_audit import gemini, vertex_extractor

    seed_farm(session, settings)

    def respond(request):
        # External-call boundary: the presenter reset the demo while the model was running.
        with session_factory() as other:
            row = other.get(ClockState, 1)
            row.job_runs = {**row.job_runs, "demo": {"generation": 1}}
            other.commit()
        return httpx.Response(
            200,
            json=gemini(
                {
                    "parcels": ["P5"],
                    "products": [COPPER],
                    "dose_value": 2,
                    "dose_unit": "kg/ha",
                    "ts_start": "2024-04-10T06:00",
                }
            ),
        )

    reply = on_message(
        deps(session_factory, settings, vertex_extractor(session_factory, respond)),
        voice("P5 copper 2 kg/ha at 6", 601),
    )

    assert "demo changed" in reply.text.lower()
    assert not reply.buttons
    assert list(session.scalars(select(Log))) == []


@pytest.mark.parametrize("parts", list(itertools.permutations(["P5", "copper", "2 kg/ha at 6"])))
def test_information_can_arrive_in_any_order(session_factory, session, settings, parts):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    for message_id, text in enumerate(parts, 701):
        reply = on_message(d, voice(text, message_id))
    assert reply.buttons, reply.text
    [log] = list(session.scalars(select(Log)))
    assert [p.alias for p in log.parcels] == ["P5"]
    assert [(q.label, float(q.value)) for q in log.quantities] == [(COPPER, 2)]


def test_clarifications_are_private_to_the_chat_and_expire(session_factory, session, settings):
    from dataclasses import replace

    from farmcore.apps.chat import conversation

    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    stamp = datetime(2026, 10, 17, 21, tzinfo=UTC)
    with patch.object(conversation, "datetime", wraps=datetime) as clock:
        clock.now.return_value = stamp
        on_message(d, voice("P5 copper", 801))
        other = on_message(d, replace(voice("2 kg/ha", 802), chat_id=123))
        assert other.text.startswith("Which parcel")
        clock.now.return_value = stamp + timedelta(minutes=16)
        reply = on_message(d, voice("2 kg/ha", 803))
        assert reply.text.startswith("Which parcel")
    assert list(session.scalars(select(Log))) == []


def test_correction_replaces_old_parcel_and_question_does_not_become_a_spray(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    on_message(d, voice("P5 copper", 901))
    question = on_message(d, voice("Should I spray P6?", 902))
    assert not question.buttons
    reply = on_message(d, voice("Actually P6, 2 kg/ha at 6", 903))
    assert reply.buttons, reply.text
    [log] = list(session.scalars(select(Log)))
    assert [p.alias for p in log.parcels] == ["P6"]


def test_vertex_receives_only_bounded_unfinished_history_and_keeps_product_rates(
    session_factory, session, settings
):
    import json

    import httpx

    from tests.apps.chat.test_audit import gemini, vertex_extractor
    from tests.apps.chat.test_handlers import SULPHUR

    seed_farm(session, settings)
    payloads = []

    def respond(request):
        body = json.loads(request.content)
        payloads.append(json.loads(body["contents"][0]["parts"][0]["text"]))
        draft = {"parcels": ["P5"], "products": [], "ts_start": None}
        if len(payloads) == 5:
            draft.update(
                products=[COPPER, SULPHUR],
                product_doses=[
                    {"product": COPPER, "value": 2, "unit": "kg/ha"},
                    {"product": SULPHUR, "value": 1, "unit": "kg/ha"},
                ],
            )
        return httpx.Response(200, json=gemini(draft))

    d = deps(session_factory, settings, vertex_extractor(session_factory, respond))
    for message_id in range(1001, 1005):
        on_message(d, voice("P5", message_id))
    reply = on_message(d, voice("copper 2 kg/ha and sulphur 1 kg/ha", 1005))

    assert "clarification" not in payloads[0]
    context = payloads[-1]["clarification"]
    assert len(context["history"]) == 6
    assert context["draft"]["parcels"] == ["P5"]
    assert {turn["role"] for turn in context["history"]} == {"user", "assistant"}
    assert "tg:" not in json.dumps(payloads)
    assert reply.buttons, reply.text
    assert f"{SULPHUR}: 1 kg/ha" in reply.text


def test_missing_second_product_dose_is_asked_for_and_remembered(
    session_factory, session, settings
):
    from tests.apps.chat.test_handlers import SULPHUR

    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    first = on_message(d, voice("P5 copper 2 kg/ha and sulphur at 6", 1101))
    assert not first.buttons, first.text
    assert f"How much {SULPHUR}" in first.text

    reply = on_message(d, voice("1 kg/ha", 1102))

    assert reply.buttons, reply.text
    [log] = list(session.scalars(select(Log)))
    assert sorted((q.label, float(q.value)) for q in log.quantities) == [(COPPER, 2), (SULPHUR, 1)]


def test_keyboard_confirmation_only_confirms_one_unambiguous_chat_owned_draft(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    on_message(d, voice("P5 copper 2 kg/ha at 6", 1201))

    reply = on_message(d, voice("/confirm", 1202))

    assert reply.text.startswith("✅ Logged:"), reply.text
    [log] = list(session.scalars(select(Log)))
    assert log.status == "done"
    again = on_message(d, voice("/confirm", 1203))
    assert not again.buttons
    assert "No single draft" in again.text
