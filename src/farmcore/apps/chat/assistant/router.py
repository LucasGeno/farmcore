"""Intent router: rules, not the LLM (free, instant, table-tested).

- *question*: it starts with a question word (EN: when, which, what, is, are, should, how, can,
  could, may, do, does, did, have, has, was, were, would, might, must, will, shall, why, where,
  who; FR: quand, quel/quelle/quels/quelles, combien, pourquoi, dois-je, puis-je, peut/peux,
  ai-je, avez-vous, est-ce, faut-il) unless it carries a dose AND a first-person subject reports
  the spray ("I sprayed", "we have treated", "j'ai traité", "on a passé"). A subject after an
  auxiliary or a modal is inverted, so it reports nothing ("Have I sprayed…", "Should I have
  sprayed…", "When could we have treated…").
  So "Should I spray P5 with copper at 2 kg/ha?", "Is P5 treated with 2 kg of copper?" and
  "Quand traiter P5 avec 2 kg de cuivre ?" stay questions: the offline assistant answers, the
  extractor (the model, with the real one) is never reached. "What a …" is an exclamation;
- *log*: else, it has a dose ("2 kg", "1,5 L/ha") or a sprayed / traité / passé verb. Whisper
  turns rising intonation into "?", so "I sprayed P1 with copper at 2 kg, is that ok?" and
  "Sprayed …?" are still drafted. Ambiguous, kept as logs on purpose (a question word first and
  a first-person report with a dose): "Is it ok that I sprayed P1 with copper at 2 kg?",
  "Est-ce que j'ai bien traité P5 avec 2 kg ?" — the draft is offered, nothing is written
  without ✅;
- *question* also: the text ends with `?`;
- else *other*: `handlers` sends it to the help text unless it `mentions` a parcel or a
  product (a log said without a verb or a dose, "P6 au cuivre ce matin", keeps the log path).

Only a question, and an *other* naming nothing on the farm, leave the existing extractor →
pending → ✅/❌ path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from farmcore.apps.chat.extractor.base import fold

QUESTION = "question"
LOG = "log"
OTHER = "other"

QUESTION_WORDS = frozenset(
    {
        *("when", "which", "what", "is", "are", "should", "how"),
        *("can", "could", "may", "do", "does", "did", "have", "has", "was", "were"),
        *("would", "might", "must", "will", "shall", "why", "where", "who"),
        *("quand", "quel", "quelle", "quels", "quelles", "combien"),
        *("dois", "puis", "peut", "peux", "ai", "avez", "pourquoi"),
    }
)
QUESTION_PREFIXES = ("est-ce", "est ce", "faut-il", "faut il")
SPRAY_VERBS = frozenset(
    {"sprayed", "treated", "applied", "traite", "traites", "traitee", "traitees", "passe"}
)
# A completed spray reported in the first person: subject, optional auxiliary or adverb, verb.
SUBJECTS = frozenset({"i", "we", "j", "je", "nous", "on"})
REPORT_FILLERS = frozenset({"have", "ve", "had", "ai", "avons", "a", "just", "already", "bien"})
REPORT_FILLERS |= {"deja"}
# Before the subject, an auxiliary or a modal inverts it into a question, perfect tense included
# ("Have I sprayed…", "Did we treat…", "Should I have sprayed…", "When could we have treated…").
AUXILIARIES = frozenset({"have", "has", "had", "did", "do", "does", "was", "were"})
MODALS = frozenset({"should", "could", "would", "might", "must", "can", "may", "will", "shall"})
INVERTING = AUXILIARIES | MODALS
_DOSE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:kg|kilos?|kilogrammes?|kilograms?|l|litres?|liters?|g)\b"
)
_WORD = re.compile(r"[a-z0-9]+(?:-[a-z]+)?")
# Words that mark a French message (accents folded); parcel names ("Les Granges") are not used.
FRENCH_WORDS = frozenset(
    {
        *("quand", "quel", "quelle", "quels", "quelles", "combien", "est-ce", "faut-il"),
        *("demain", "aujourd", "hier", "meteo", "pluie", "vent", "temps"),
        *("traite", "traiter", "traitement", "traitements", "dernier", "derniere"),
        *("protege", "protegee", "proteges", "parcelle", "parcelles"),
        *("je", "ai", "ai-je", "dois-je", "quoi", "que", "est", "il", "depuis", "sur"),
        *("bonjour", "salut", "merci"),
    }
)


def _words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def intent(text: str) -> str:
    """`question`, `log` or `other` for one message or voice transcript."""
    stripped = text.strip()
    folded = fold(stripped)
    words = _words(stripped)
    first = words[0].split("-")[0] if words else ""
    asks = (first in QUESTION_WORDS or folded.startswith(QUESTION_PREFIXES)) and not (
        folded.startswith(("what a ", "what an "))  # an exclamation
    )
    dose = _DOSE.search(folded) is not None
    if asks and not (dose and _reports(words)):
        return QUESTION
    if dose or SPRAY_VERBS & set(words):
        return LOG
    if asks or stripped.endswith("?"):
        return QUESTION
    return OTHER


def _reports(words: list[str]) -> bool:
    """Whether a first-person subject reports a completed spray ("i sprayed", "j ai bien
    traite"), not inverted into a question ("have i sprayed")."""
    for i, word in enumerate(words):
        if word not in SUBJECTS or (i and words[i - 1] in INVERTING):
            continue
        j = i + 1
        while j < len(words) and words[j] in REPORT_FILLERS:
            j += 1
        if j < len(words) and words[j] in SPRAY_VERBS:
            return True
    return False


def mentions(text: str, phrases: Iterable[str]) -> bool:
    """Whether `text` names one of `phrases` as whole words (case and accents ignored)."""
    padded = f" {' '.join(_words(text))} "
    return any(key and f" {key} " in padded for key in (" ".join(_words(p)) for p in phrases if p))


def language(text: str) -> str:
    """`fr` when the message has a French marker word, else `en`."""
    return "fr" if FRENCH_WORDS & set(_words(text)) else "en"
