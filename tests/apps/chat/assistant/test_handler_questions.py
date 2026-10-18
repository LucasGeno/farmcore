"""The question branch of `handlers.on_message`: the four demo questions are answered offline by
the keyword fallback over the apps' tools, with "Open card" URL buttons, and nothing is logged.
A spray sentence still becomes a pending log echoed with ✅ / ❌.

Real seeds (Scene B) and real manifests, replayed to 10 Apr 2024 06:00 with P5 and P6 sprayed at
05:30 and confirmed (the hero loop's state after ✅)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from farmcore.apps.chat.dto import Incoming
from farmcore.apps.chat.extractor import RegexExtractor
from farmcore.apps.chat.handlers import ChatDeps, on_message
from farmcore.core import jobs
from farmcore.core.models.canonical import AccessLog, Advice, Log, Parcel
from farmcore.core.models.kernel import AppModule
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local
from tests.apps.chat.assistant.toolkit import CHAT, write_counts

pytestmark = pytest.mark.data

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

BASE = "https://farm.example.org"
NOW = "2024-04-10 06:00"
SPEC_SENTENCE = (
    "Sprayed Les Granges and Peyrot this morning with Bouillie bordelaise, 2 kilos per hectare"
)


@pytest.fixture
def hero(session, settings, session_factory, manifests, seeded, step):
    seeded("B")
    step("2024-04-08 06:00")
    step(NOW)
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 05:30"), "copper-contact")
    run = jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)
    assert run is not None and run.ok
    session.expire_all()


@pytest.fixture
def deps(session_factory, settings, manifests) -> ChatDeps:
    public = settings.model_copy(update={"public_base_url": BASE})
    return ChatDeps(session_factory, RegexExtractor(), public, tuple(manifests))


def text(message: str, message_id: int = 1) -> Incoming:
    return Incoming(chat_id=CHAT, text=message, transcript=None, message_id=message_id)


def voice(transcript: str, message_id: int = 1) -> Incoming:
    return Incoming(chat_id=CHAT, text=None, transcript=transcript, message_id=message_id)


def parcel_id(session, alias: str) -> int:
    return session.scalar(select(Parcel.id).where(Parcel.alias == alias))


def headline(session, alias: str) -> str:
    row = session.scalar(
        select(Advice)
        .where(Advice.parcel_id == parcel_id(session, alias))
        .order_by(Advice.evaluated_at.desc(), Advice.id.desc())
        .limit(1)
    )
    return row.props["headline"]


def pending(session) -> int:
    return session.scalar(select(func.count()).select_from(Log).where(Log.status == "pending"))


def assistant_rows(session) -> int:
    return session.scalar(
        select(func.count()).select_from(AccessLog).where(AccessLog.purpose == "assistant")
    )


def test_is_peyrot_protected_quotes_the_headline_with_an_open_card_button(session, hero, deps):
    before = write_counts(session)

    out = on_message(deps, text("Is Peyrot protected?"))

    assert out.text.splitlines() == ["P6 Peyrot: protected.", headline(session, "P6")]
    farm_id = session.get(Parcel, parcel_id(session, "P6")).farm_id
    assert out.url_buttons == [
        (
            "Open card P6",
            f"{BASE}/advisor/parcels/{parcel_id(session, 'P6')}?farm={farm_id}",
        )
    ]
    assert out.buttons == []
    assert pending(session) == 0
    assert write_counts(session) == before


@pytest.mark.parametrize(
    ("question", "first_line"),
    [
        ("When did I last spray Le Plateau?", "No spray is logged on P1 Le Plateau yet."),
        (
            "When did I last spray Peyrot?",
            "P6 Peyrot was last sprayed on 10 Apr 05:30: copper, 2 kg/ha.",
        ),
        ("What do I spray today?", "8 parcels to spray: P1, P2, P3, P4, P7, P8, P9, P10."),
        ("Is Peyrot protected?", "P6 Peyrot: protected."),
        ("Weather tomorrow?", "Weather tomorrow (11 Apr), station St-Émilion 33394002:"),
    ],
)
def test_the_four_demo_questions_are_answered_in_three_lines(
    session, hero, deps, question, first_line
):
    before = assistant_rows(session)

    out = on_message(deps, text(question))

    lines = out.text.splitlines()
    assert lines[0].startswith(first_line)
    assert len(lines) <= 3
    assert out.url_buttons
    assert all(url.startswith(BASE + "/") for _, url in out.url_buttons)
    assert pending(session) == 0
    assert assistant_rows(session) > before


def test_a_voice_question_takes_the_same_branch(session, hero, deps):
    out = on_message(deps, voice("what do I spray today"))

    assert out.text.startswith("8 parcels to spray:")
    assert pending(session) == 0


def test_a_french_question_gets_a_french_answer(session, hero, deps):
    out = on_message(deps, text("Est-ce que Peyrot est protégé ?"))

    assert out.text.splitlines()[0] == "P6 Peyrot : protégée."


def test_a_question_no_tool_covers(session, hero, deps):
    out = on_message(deps, text("What is the price of wine?"))

    assert out.text.startswith("I can't answer that yet; try")
    assert out.buttons == [] and out.url_buttons == []
    assert pending(session) == 0


def test_a_parcel_question_without_a_parcel_asks_which_one(session, hero, deps):
    out = on_message(deps, text("Is it protected?"))

    assert "P6 Peyrot" in out.text
    assert out.text.startswith("I don't know that parcel.")


def test_without_tools_a_question_is_not_answered(session, hero, deps):
    bare = ChatDeps(deps.session_factory, deps.extractor, deps.settings, ())

    out = on_message(bare, text("Is Peyrot protected?"))

    assert out.text.startswith("I can't answer that yet")


def test_the_demo_spray_sentence_still_drafts_a_pending_log(session, hero, deps):
    out = on_message(deps, voice(SPEC_SENTENCE, message_id=9))

    assert pending(session) == 1
    assert out.text.splitlines()[0].endswith("— confirm?")
    assert [label for label, _ in out.buttons] == ["✅ Confirm", "❌ Cancel"]


def test_on_a_local_base_url_the_links_go_in_the_text(session, hero, settings, deps):
    local_deps = ChatDeps(deps.session_factory, deps.extractor, settings, deps.manifests)

    out = on_message(local_deps, text("Is Peyrot protected?"))

    assert out.url_buttons == []
    farm_id = session.get(Parcel, parcel_id(session, "P6")).farm_id
    assert out.text.splitlines()[-1] == (
        "Open card P6: "
        f"http://localhost:8000/advisor/parcels/{parcel_id(session, 'P6')}?farm={farm_id}"
    )


def test_a_spray_that_reads_like_a_question_still_drafts_a_pending_log(session, hero, deps):
    out = on_message(deps, voice("I sprayed P1 with copper at 2 kg, is that ok?", message_id=11))

    assert out.text.splitlines()[0].endswith("— confirm?")
    assert pending(session) == 1
    assert [label for label, _ in out.buttons] == ["✅ Confirm", "❌ Cancel"]


def test_a_spray_with_a_rising_question_mark_takes_the_log_path(session, hero, deps):
    # Whisper's "?" on rising intonation: the extractor asks for the missing dose, not the
    # assistant's "I can't answer that yet".
    out = on_message(deps, voice("Traité P6 au cuivre ce matin ?", message_id=12))

    assert out.text.startswith("How much did you use?")


def test_farm_wide_protection_answers_with_todays_advice(session, hero, deps):
    out = on_message(deps, text("Are my parcels protected?"))

    assert out.text.startswith("8 parcels to spray:")
    assert out.text.splitlines()[-1] == "Others: 2 protected."
    assert pending(session) == 0


class _NoCall:
    """An extractor that must not be reached (a greeting is not a spray)."""

    def extract(self, req):  # pragma: no cover - failing is the point
        raise AssertionError("the extractor was called")


@pytest.mark.parametrize("greeting", ["hello", "thanks", "ok"])
def test_a_greeting_gets_the_help_text_not_a_parcel_prompt(session, hero, deps, greeting):
    quiet = ChatDeps(deps.session_factory, _NoCall(), deps.settings, deps.manifests)

    out = on_message(quiet, text(greeting))

    assert "Which parcel" not in out.text
    assert "P1 copper, 2 kg, this morning" in out.text  # how to log
    assert "Is Peyrot protected?" in out.text  # what to ask
    assert len(out.text.splitlines()) <= 3
    assert out.buttons == [] and pending(session) == 0


def test_a_french_greeting_gets_the_french_help_text(session, hero, deps):
    quiet = ChatDeps(deps.session_factory, _NoCall(), deps.settings, deps.manifests)

    out = on_message(quiet, text("Bonjour"))

    assert "Est-ce que Peyrot est protégé ?" in out.text


def test_a_log_without_a_verb_or_dose_that_names_a_parcel_still_takes_the_log_path(
    session, hero, deps
):
    # "other" by the router, but it names a parcel and a product: the extractor asks for the dose.
    out = on_message(deps, voice("P6 au cuivre ce matin", message_id=13))

    assert out.text.startswith("How much did you use?")


def test_an_app_switched_off_on_apps_loses_its_tool_without_a_restart(session, hero, deps):
    asked = "Weather tomorrow?"
    assert on_message(deps, text(asked)).text.startswith("Weather tomorrow (11 Apr)")
    session.merge(AppModule(name="stations", state="uninstalled", version="0.1.0"))
    session.commit()

    out = on_message(deps, text(asked, message_id=2))

    assert "station St-Émilion" not in out.text
    assert pending(session) == 0


@pytest.mark.parametrize(
    "question",
    [
        "Should I spray P5 with copper at 2 kg/ha?",
        "Quand traiter P5 avec 2 kg de cuivre ?",
        "Is P5 treated with 2 kg of copper?",
        "Are Les Granges sprayed with 2 kg?",
        "Est-ce que P5 est traité avec 2 kg de cuivre ?",
        # Final review: modal-inverted perfect questions.
        "Should I have sprayed P5 with 2 kg of copper?",
        "Could we have treated P5 with 2 kg?",
        "When should I have sprayed P5 with 2 kg?",
        "Why did we spray P5 with 2 kg of copper",
    ],
)
def test_a_question_with_a_dose_never_reaches_the_extractor(session, hero, deps, question):
    # Review R4: these went to the extractor (the model with the real one) and drafted a log.
    quiet = ChatDeps(deps.session_factory, _NoCall(), deps.settings, deps.manifests)
    before = write_counts(session)

    out = on_message(quiet, text(question))

    assert out.text and out.buttons == []
    assert pending(session) == 0
    assert write_counts(session) == before


@pytest.mark.parametrize(
    "question",
    [
        f"{wh}{aux} {subject}{perfect} {verb} P5 with 2 kg of copper{mark}"
        for wh, aux, perfect, verb in [
            ("", "Should", " have", "sprayed"),
            ("When ", "could", " have", "treated"),
            ("", "Did", "", "spray"),
            ("Why ", "have", "", "applied"),
        ]
        for subject in ("I", "we")
        for mark in ("?", "")
    ],
)
def test_an_inverted_question_never_reaches_the_extractor(session, hero, deps, question):
    quiet = ChatDeps(deps.session_factory, _NoCall(), deps.settings, deps.manifests)
    before = write_counts(session)

    out = on_message(quiet, text(question))

    assert out.text and out.buttons == []
    assert pending(session) == 0 and write_counts(session) == before


@pytest.mark.parametrize(
    "report",
    [
        "I sprayed P1 with copper at 2 kg, is that ok?",
        "We have treated Le Plateau with copper, 2 kg",
        "J'ai traité Le Plateau à la bouillie bordelaise, 2 kilos par hectare",
    ],
)
def test_a_completed_report_with_a_check_still_drafts_a_log(session, hero, deps, report):
    out = on_message(deps, text(report, message_id=21))

    assert out.buttons  # the ✅/❌ draft
    assert pending(session) == 1
