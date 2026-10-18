"""Intent router: rules, not the LLM. Questions go to the assistant; logs and the rest keep the
extractor → pending → ✅/❌ path."""

from __future__ import annotations

import pytest

from farmcore.apps.chat.assistant import router

# The four demo questions , typed and as Whisper may hear them (no "?").
QUESTIONS = [
    "When did I last spray Le Plateau?",
    "What do I spray today?",
    "Is Peyrot protected?",
    "Weather tomorrow?",
    "what do I spray today",
    "is Peyrot protected",
    "Which parcels should I spray today?",
    "Should I spray Peyrot today",
    "How much rain since the spray",
    "Are my parcels protected",
    "Quand ai-je traité Le Plateau ?",
    "Quelle météo demain ?",
    "Quels traitements aujourd'hui",
    "Est-ce que Peyrot est protégé",
    "Faut-il traiter Les Granges",
    "Combien de pluie depuis le traitement",
    "météo demain ?",
]

# The demo voice sentences (tests/e2e/test_hero_loop.py, tests/apps/chat/test_handlers.py).
LOGS = [
    "Sprayed Les Granges and Peyrot this morning with Bouillie bordelaise, 2 kilos per hectare",
    "Traité Les Granges et Peyrot ce matin à la bouillie bordelaise, 2 kilos par hectare",
    "Sprayed Les Granges and Peyrot with copper and sulphur, 2 kilos, this morning",
    "copper + sulphur, 2 kg, this morning",
    "P1 copper 2 kg",
    "P1 copper, 2 kg",
    "Passé du soufre sur P5 ce matin",
    "P6 traité au cuivre 1,5 L/ha",
    # A spray with a dose or a past-tense verb is a log even when it reads like a question:
    # Whisper adds "?" to rising intonation, and the farmer may ask for a check in the same breath.
    "I sprayed P1 with copper at 2 kg, is that ok?",
    "What a morning: sprayed P1 copper 2 kg",
    "P1 copper 2 kg?",
    "Traité P1 ?",
    "Sprayed Peyrot this morning?",
]

OTHER = ["hello", "echo", "thanks", "bonjour"]

# A question word first and no dose: a question, even with a spray verb inside.
QUESTIONS_WITH_A_SPRAY_VERB = [
    "Quand ai-je traité Le Plateau ?",
    "When was P1 sprayed?",
    "Est-ce que P6 a été traité ?",
]


@pytest.mark.parametrize("text", QUESTIONS)
def test_questions(text: str) -> None:
    assert router.intent(text) == router.QUESTION


@pytest.mark.parametrize("text", LOGS)
def test_logs(text: str) -> None:
    assert router.intent(text) == router.LOG


@pytest.mark.parametrize("text", OTHER)
def test_other(text: str) -> None:
    assert router.intent(text) == router.OTHER


@pytest.mark.parametrize("text", QUESTIONS_WITH_A_SPRAY_VERB)
def test_a_question_word_first_without_a_dose_stays_a_question(text: str) -> None:
    assert router.intent(text) == router.QUESTION


def test_a_log_with_a_question_word_inside_is_still_a_log() -> None:
    # Only the first word counts: "what" in the middle does not make a question.
    assert router.intent("Sprayed P1 copper 2 kg, what a morning") == router.LOG


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Is Peyrot protected?", "en"),
        ("Weather tomorrow?", "en"),
        ("Quelle météo demain ?", "fr"),
        ("Est-ce que Peyrot est protégé", "fr"),
        ("Quand ai-je traité Le Plateau ?", "fr"),
    ],
)
def test_language(text: str, lang: str) -> None:
    assert router.language(text) == lang


# Review R4: a genuine question that carries a dose is still a question (offline assistant, no
# extractor): it asks about a spray that has not happened, or asks *when* one did.
QUESTIONS_WITH_A_DOSE = [
    "Should I spray P5 with copper at 2 kg/ha?",
    "Should I spray P5 with copper at 2 kg/ha",  # Whisper, no "?"
    "Can I spray Peyrot with 1,5 L/ha of sulphur today?",
    "Do I need 2 kg of copper on P1?",
    "When did I spray P1 with 2 kg of copper?",
    "When was P1 sprayed with 2 kg?",
    "Quand traiter P5 avec 2 kg de cuivre ?",
    "Faut-il traiter P5 avec 2 kg de cuivre",
    "Est-ce que P6 a été traité avec 2 kg ?",
    "Quand ai-je traité P5 avec 2 kg ?",
    "Dois-je traiter P5 avec 2 kg de cuivre ?",
    # Re-review: a status question (present / passive) with a dose asserts no spray.
    "Is P5 treated with 2 kg of copper?",
    "Are Les Granges sprayed with 2 kg?",
    "Est-ce que P5 est traité avec 2 kg de cuivre ?",
    "Is P5 sprayed with 2 kg",  # Whisper, no "?"
    "Have I sprayed P5 with 2 kg of copper?",
    "Was P5 sprayed with 2 kg?",
    "Ai-je traité P5 avec 2 kg ?",
]

# Ambiguous on purpose, and kept on the log path: a question word first, then a completed spray
# with a dose said as a statement. The draft is offered and nothing is written without ✅.
# The rule: a question word first stays a question unless a first-person subject reports the
# spray ("I sprayed", "we have treated", "j'ai traité", "on a passé"); "What a …" is an
# exclamation, not a question.
AMBIGUOUS_LOGS = [
    "What a morning: sprayed P1 copper 2 kg",
    "Is it ok that I sprayed P1 with copper at 2 kg?",
    "Is that fine, we have treated P5 with 2 kg of copper",
    "Est-ce que j'ai bien traité P5 avec 2 kg ?",
]


@pytest.mark.parametrize("text", QUESTIONS_WITH_A_DOSE)
def test_a_question_with_a_dose_stays_a_question(text: str) -> None:
    assert router.intent(text) == router.QUESTION


@pytest.mark.parametrize("text", AMBIGUOUS_LOGS)
def test_a_completed_spray_after_a_question_word_is_still_a_log(text: str) -> None:
    assert router.intent(text) == router.LOG


# Final review: an auxiliary or modal before the subject inverts it into a question, perfect
# tense included ("Should I have sprayed…", "When could we have treated…"). The grammatical
# category, not literal strings: every supported inverting word × I/we × completed spray verb,
# with and without a question word first, with and without Whisper's "?".
INVERTING_WORDS = ["Should", "Could", "Would", "Might", "Must", "Can", "Will", "Did", "Have"]
WH_PREFIXES = ["", "When ", "Why "]
SUBJECTS = ["I", "we"]
DONE_VERBS = ["sprayed", "treated", "applied"]


def _inverted(wh: str, aux: str, subject: str, verb: str, mark: str) -> str:
    perfect = "" if aux in ("Did", "Have") else " have"
    head = f"{wh}{aux.lower() if wh else aux}"
    if aux == "Did":
        verb = {"sprayed": "spray", "treated": "treat", "applied": "apply"}[verb]
    return f"{head} {subject}{perfect} {verb} P5 with 2 kg of copper{mark}"


INVERTED_QUESTIONS = [
    _inverted(wh, aux, subject, verb, mark)
    for wh in WH_PREFIXES
    for aux in INVERTING_WORDS
    for subject in SUBJECTS
    for verb in DONE_VERBS
    for mark in ("?", "")
]

# Their completed first-person counterparts, EN and FR, stay logs.
COMPLETED_REPORTS = [
    f"{subject} {aux}{verb} P5 with 2 kg of copper{mark}"
    for subject in SUBJECTS
    for aux in ("", "have ")
    for verb in DONE_VERBS
    for mark in ("", "?")
] + [
    "J'ai traité P5 avec 2 kg de cuivre",
    "J'ai traité P5 avec 2 kg de cuivre ?",
    "Nous avons traité P5 avec 2 kg de cuivre",
    "On a passé du soufre sur P5, 2 kg",
    "Je viens de traiter P5 avec 2 kg de cuivre",
]


@pytest.mark.parametrize("text", INVERTED_QUESTIONS)
def test_an_inverted_subject_with_a_dose_is_a_question(text: str) -> None:
    assert router.intent(text) == router.QUESTION


@pytest.mark.parametrize("text", COMPLETED_REPORTS)
def test_a_completed_first_person_report_with_a_dose_is_a_log(text: str) -> None:
    assert router.intent(text) == router.LOG
