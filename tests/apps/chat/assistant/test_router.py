"""Intent router: rules, not the LLM. Questions go to the assistant; logs and the rest keep the
extractor → pending → ✅/❌ path.

One row per equivalence class; each id names the regression the row catches."""

from __future__ import annotations

import pytest

from farmcore.apps.chat.assistant import router

P = pytest.param

QUESTIONS = [
    # The demo questions, typed and as Whisper hears them (no "?").
    P("When did I last spray Le Plateau?", id="en-wh-word-with-spray-verb"),
    P("What do I spray today?", id="en-wh-word"),
    P("what do I spray today", id="whisper-no-mark-lowercase"),
    P("Is Peyrot protected?", id="en-auxiliary-first"),
    P("Weather tomorrow?", id="no-question-word-but-mark"),
    P("météo demain ?", id="fr-no-question-word-but-spaced-mark"),
    P("Quels traitements aujourd'hui", id="fr-wh-word-no-mark"),
    P("Est-ce que Peyrot est protégé", id="fr-est-ce-prefix"),
    P("Faut-il traiter Les Granges", id="fr-faut-il-prefix-with-infinitive"),
    # A question word first and no dose: a question, even with a past-tense spray verb inside.
    P("When was P1 sprayed?", id="en-wh-word-passive-spray-verb"),
    P("Quand ai-je traité Le Plateau ?", id="fr-wh-word-inverted-ai-je"),
]

LOGS = [
    P(
        "Sprayed Les Granges and Peyrot this morning with Bouillie bordelaise, 2 kilos per hectare",
        id="en-demo-sentence",
    ),
    P(
        "Traité Les Granges et Peyrot ce matin à la bouillie bordelaise, 2 kilos par hectare",
        id="fr-demo-sentence",
    ),
    P("P1 copper 2 kg", id="dose-without-verb"),
    P("P6 traité au cuivre 1,5 L/ha", id="decimal-comma-litres-dose"),
    P("Passé du soufre sur P5 ce matin", id="fr-verb-without-dose"),
    # Whisper turns rising intonation into "?": a dose or a spray verb still drafts a log.
    P("P1 copper 2 kg?", id="dose-beats-mark"),
    P("Sprayed Peyrot this morning?", id="en-verb-beats-mark"),
    P("Traité P1 ?", id="fr-verb-beats-mark"),
    P("I sprayed P1 with copper at 2 kg, is that ok?", id="report-then-check-in-one-breath"),
    # Only the first word counts: "what" in the middle does not make a question.
    P("Sprayed P1 copper 2 kg, what a morning", id="question-word-not-first"),
]


@pytest.mark.parametrize("text", QUESTIONS)
def test_questions(text: str) -> None:
    assert router.intent(text) == router.QUESTION


@pytest.mark.parametrize("text", LOGS)
def test_logs(text: str) -> None:
    assert router.intent(text) == router.LOG


def test_small_talk_is_other() -> None:
    assert router.intent("hello") == router.OTHER


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        P("Is Peyrot protected?", "en", id="parcel-name-is-not-a-french-marker"),
        P("Quelle météo demain ?", "fr", id="accented-marker-folded"),
        P("Est-ce que Peyrot est protégé", "fr", id="hyphenated-marker"),
    ],
)
def test_language(text: str, lang: str) -> None:
    assert router.language(text) == lang


# A genuine question that carries a dose is still a question (offline assistant, no extractor):
# it asks about a spray that has not happened, asks *when* one did, or asks about a status.
QUESTIONS_WITH_A_DOSE = [
    P("Should I spray P5 with copper at 2 kg/ha", id="en-modal-future-spray-whisper"),
    P("Do I need 2 kg of copper on P1?", id="en-do-with-other-verb"),
    P("When was P1 sprayed with 2 kg?", id="en-wh-word-passive"),
    P("Is P5 treated with 2 kg of copper?", id="en-status-present-passive"),
    P("Quand traiter P5 avec 2 kg de cuivre ?", id="fr-wh-word-infinitive"),
    P("Faut-il traiter P5 avec 2 kg de cuivre", id="fr-faut-il-whisper"),
    P("Dois-je traiter P5 avec 2 kg de cuivre ?", id="fr-dois-je"),
    P("Est-ce que P5 est traité avec 2 kg de cuivre ?", id="fr-status-passive"),
    P("Ai-je traité P5 avec 2 kg ?", id="fr-inverted-ai-je-first"),
    P("Quand ai-je traité P5 avec 2 kg ?", id="fr-inverted-ai-je-after-wh-word"),
]

# Ambiguous on purpose, and kept on the log path: a question word first, then a first-person
# subject reporting a completed spray with a dose. The draft is offered and nothing is written
# without ✅. "What a …" is an exclamation, not a question.
AMBIGUOUS_LOGS = [
    P("What a morning: sprayed P1 copper 2 kg", id="what-a-is-an-exclamation"),
    P("Is it ok that I sprayed P1 with copper at 2 kg?", id="en-subject-then-verb"),
    P("Is that fine, we have treated P5 with 2 kg of copper", id="en-subject-auxiliary-verb"),
    P("Est-ce que j'ai bien traité P5 avec 2 kg ?", id="fr-subject-auxiliary-adverb-verb"),
]

# Completed first-person reports with a dose stay logs, Whisper's "?" included.
COMPLETED_REPORTS = [
    P("I have sprayed P5 with 2 kg of copper?", id="en-perfect-with-mark"),
    P("we applied P5 with 2 kg of copper", id="en-simple-past-lowercase"),
    P("J'ai traité P5 avec 2 kg de cuivre ?", id="fr-perfect-with-mark"),
    P("On a passé du soufre sur P5, 2 kg", id="fr-on-a-passe"),
]


@pytest.mark.parametrize("text", QUESTIONS_WITH_A_DOSE)
def test_a_question_with_a_dose_stays_a_question(text: str) -> None:
    assert router.intent(text) == router.QUESTION


@pytest.mark.parametrize("text", AMBIGUOUS_LOGS)
def test_a_completed_spray_after_a_question_word_is_still_a_log(text: str) -> None:
    assert router.intent(text) == router.LOG


@pytest.mark.parametrize("text", COMPLETED_REPORTS)
def test_a_completed_first_person_report_with_a_dose_is_a_log(text: str) -> None:
    assert router.intent(text) == router.LOG


# An auxiliary or modal before the subject inverts it into a question, perfect tense included
# ("Should I have sprayed…", "When could we have treated…"). A pairwise covering list: every
# inverting word appears with and without a question word first, and every pair of values of
# (question word, inverting word, subject, verb, "?") appears together at least once.
INVERTED_QUESTIONS = [
    "Should I have sprayed P5 with 2 kg of copper?",
    "When should we have applied P5 with 2 kg of copper",
    "Why should I have treated P5 with 2 kg of copper?",
    "Could we have treated P5 with 2 kg of copper?",
    "When could I have sprayed P5 with 2 kg of copper",
    "Why could we have applied P5 with 2 kg of copper?",
    "Would I have applied P5 with 2 kg of copper?",
    "When would we have treated P5 with 2 kg of copper",
    "Why would I have sprayed P5 with 2 kg of copper?",
    "Might we have sprayed P5 with 2 kg of copper",
    "When might I have applied P5 with 2 kg of copper?",
    "Why might we have treated P5 with 2 kg of copper",
    "Must I have treated P5 with 2 kg of copper",
    "When must we have sprayed P5 with 2 kg of copper?",
    "Why must I have applied P5 with 2 kg of copper",
    "Can we have applied P5 with 2 kg of copper",
    "When can I have treated P5 with 2 kg of copper?",
    "Why can we have sprayed P5 with 2 kg of copper",
    "Will I have sprayed P5 with 2 kg of copper?",
    "When will we have applied P5 with 2 kg of copper",
    "Why will I have treated P5 with 2 kg of copper?",
    "Did we treat P5 with 2 kg of copper?",
    "When did I spray P5 with 2 kg of copper",
    "Why did we apply P5 with 2 kg of copper?",
    "Have I applied P5 with 2 kg of copper?",
    "When have we treated P5 with 2 kg of copper",
    "Why have I sprayed P5 with 2 kg of copper?",
]


@pytest.mark.parametrize("text", INVERTED_QUESTIONS)
def test_an_inverted_subject_with_a_dose_is_a_question(text: str) -> None:
    assert router.intent(text) == router.QUESTION
