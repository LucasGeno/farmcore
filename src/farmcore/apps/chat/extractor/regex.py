"""Deterministic fallback parser for short spray notes in English and French.

Covers the demo sentences: parcel aliases ("P1", "P-1") and names ("Les Granges"), several per
sentence; copper/cuivre/(bouillie) bordelaise and sulphur/sulfur/soufre, or any other shortlist
active substance said verbatim, mapped to the shortlist product with that active substance; a
shortlist name said in full or by its distinctive first word ("Microthiol"); a dose number, in
digits or spelled out (one..ten, un..dix), + kg|kilos|L (optionally /ha); "this morning"/"ce
matin" (08:00 today) and "yesterday"/"hier" (08:00 the day before), overridden by a clock time
("at 6", "à 6h", "17:30"). Nothing leaves the host.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from farmcore.apps.chat.extractor.base import (
    DraftOperation,
    ExtractRequest,
    ProductDose,
    finalize,
    fold,
)

ADAPTER = "regex"
# A parse by keyword rules: plausible, never certain.
CONFIDENCE = 0.5
# "This morning" / "yesterday" without a clock time: the start of a usual spray round.
MORNING = time(8, 0)

# "Bouillie bordelaise" (Bordeaux mixture) is copper whatever the brand: "bordelaise" alone, or
# with "bouillie" (the copper product's first word), maps to the shortlist copper product.
SUBSTANCE_WORDS: dict[str, tuple[str, ...]] = {
    "copper": ("copper", "cuivre", "bordelaise"),
    "sulphur": ("sulphur", "sulfur", "soufre"),
}

# Spoken doses Whisper may leave as words; "six" is both languages.
NUMBER_WORDS: dict[str, int] = {
    **dict.fromkeys(("one", "un", "une"), 1),
    **dict.fromkeys(("two", "deux"), 2),
    **dict.fromkeys(("three", "trois"), 3),
    **dict.fromkeys(("four", "quatre"), 4),
    **dict.fromkeys(("five", "cinq"), 5),
    "six": 6,
    **dict.fromkeys(("seven", "sept"), 7),
    **dict.fromkeys(("eight", "huit"), 8),
    **dict.fromkeys(("nine", "neuf"), 9),
    **dict.fromkeys(("ten", "dix"), 10),
}
# A shortlist name's first word stands for it only when it is this long and not shared.
MIN_FIRST_WORD = 4

_ALIAS = re.compile(r"\bp[\s-]?(\d{1,3})\b")
_NUMBER = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_DOSE = re.compile(
    rf"(?<![\w.,])(\d+(?:[.,]\d+)?|(?:{_NUMBER})\s)\s*"
    r"(kg|kilos?|kilogrammes?|kilograms?|l|litres?|liters?)\b(?:\s*/\s*ha\b)?"
)
# "at 6", "at 17:30", "à 6h", "à 6h30", "vers 7 heures", "at 6 am". A bare "à 6" is not a time
# (French "à 2 kilos" is a dose), and neither is any number followed by a dose unit.
_CLOCK = re.compile(
    r"\b(at|around|a|vers)\s+(\d{1,2})"
    r"(?:\s*(?::|h)\s*(\d{2})\b|\s*(h|heures?|o'?clock|am|pm)\b)?"
    r"(?!\d|[.,]\d|\s*(?:kg|kilo|l\b|lit))"
)
_FRENCH_CLOCK_WORDS = ("a", "vers")
_UNITS = {"kg": "kg/ha", "l": "L/ha"}
_THIS_MORNING = re.compile(r"\b(this morning|ce matin)\b")
_YESTERDAY = re.compile(r"\b(yesterday|hier)\b")


def _word(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(fold(phrase))}(?!\w)")


class RegexExtractor:
    def extract(self, req: ExtractRequest) -> DraftOperation:
        text = fold(req.transcript)
        draft = DraftOperation(
            type="spray",
            parcels=_parcels(text, req),
            products=_products(text, req),
            ts_start=_ts_start(text, req),
            confidence=CONFIDENCE,
            adapter=ADAPTER,
        )
        dose = _DOSE.search(text)
        if dose:
            number = dose.group(1).strip()
            draft.dose_value = float(NUMBER_WORDS.get(number) or number.replace(",", "."))
            draft.dose_unit = _UNITS["kg" if dose.group(2).startswith("k") else "l"]
        # Multiple stated rates belong to the product immediately before them. Never copy the
        # first rate over the other products when the farmer gave separate rates.
        doses = list(_DOSE.finditer(text))
        product_matches = sorted(
            (m.start(), m.end(), name)
            for phrase, name in _product_phrases(req)
            for m in _word(phrase).finditer(text)
        )
        # "copper 2 kg and sulphur" leaves sulphur's rate unknown. A single rate after
        # a product list ("copper and sulphur, 2 kg") retains the existing shared-rate form.
        interleaved = doses and product_matches and doses[0].end() < product_matches[-1][0]
        if len(doses) > 1 or interleaved:
            rates = {}
            for match in doses:
                before = [p for p in product_matches if p[1] <= match.start()]
                if before:
                    number = match.group(1).strip()
                    name = before[-1][2]
                    rates[name] = ProductDose(
                        product=name,
                        value=float(NUMBER_WORDS.get(number) or number.replace(",", ".")),
                        unit=_UNITS["kg" if match.group(2).startswith("k") else "l"],
                    )
            draft.product_doses = list(rates.values())
        return finalize(draft, req)


def _parcels(text: str, req: ExtractRequest) -> list[str]:
    found: list[tuple[int, str]] = [
        (m.start(), f"P{int(m.group(1))}") for m in _ALIAS.finditer(text)
    ]
    for p in req.parcels:
        found += [(m.start(), p.alias) for m in _word(p.name).finditer(text)]
    return [said for _, said in sorted(found)]


def _products(text: str, req: ExtractRequest) -> list[str]:
    found: list[tuple[int, str]] = []
    for phrase, name in _product_phrases(req):
        found += [(m.start(), name) for m in _word(phrase).finditer(text)]
    return [said for _, said in sorted(found)]


def _product_phrases(req: ExtractRequest) -> list[tuple[str, str]]:
    """(spoken phrase, draft product) pairs; the draft product is a shortlist name, or the bare
    substance when the shortlist lacks it (finalize then drops it with a note)."""
    phrases = [(p.name, p.name) for p in req.products]
    known_words = {w for words in SUBSTANCE_WORDS.values() for w in words}
    for substance, words in SUBSTANCE_WORDS.items():
        phrases += [(w, _shortlist_name(substance, req) or substance) for w in words]
    for p in req.products:
        substance = fold(p.active_substance).strip()
        if substance and substance not in known_words:
            known_words.add(substance)
            phrases.append((substance, p.name))
    first_words = [fold(p.name).split()[0] if p.name.strip() else "" for p in req.products]
    for p, first in zip(req.products, first_words, strict=True):
        distinctive = len(first) >= MIN_FIRST_WORD and first.isalpha()
        if distinctive and first_words.count(first) == 1 and first not in known_words:
            phrases.append((first, p.name))
    return phrases


def _shortlist_name(substance: str, req: ExtractRequest) -> str | None:
    words = SUBSTANCE_WORDS[substance]
    for p in req.products:
        if fold(p.active_substance) in words:
            return p.name
    return None


def _ts_start(text: str, req: ExtractRequest) -> datetime | None:
    tz = req.now_local.tzinfo
    clock = _clock(text)
    if _YESTERDAY.search(text):
        return datetime.combine(req.today - timedelta(days=1), clock or MORNING, tzinfo=tz)
    if _THIS_MORNING.search(text) or clock:
        return datetime.combine(req.today, clock or MORNING, tzinfo=tz)
    return None


def _clock(text: str) -> time | None:
    for m in _CLOCK.finditer(text):
        word, hour, minute, marker = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if word in _FRENCH_CLOCK_WORDS and minute is None and marker is None:
            continue
        if marker == "pm" and hour < 12:
            hour += 12
        if hour <= 23 and int(minute or 0) <= 59:
            return time(hour, int(minute or 0))
    return None
