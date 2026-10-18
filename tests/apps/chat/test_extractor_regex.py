"""Regex extractor: the demo sentences (EN + FR) parse to drafts with shortlist product names."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.chat.extractor import DraftOperation, ExtractRequest, RegexExtractor

PARIS = ZoneInfo("Europe/Paris")
COPPER = "Copper Shield 20 WP"
SULPHUR = "Sulfo Jet 80 WG"
TEN_AM = datetime(2024, 4, 10, 10, 0, tzinfo=PARIS)


def make_request(transcript: str, now_local: datetime = TEN_AM) -> ExtractRequest:
    return ExtractRequest(
        transcript=transcript,
        parcels=[
            {"alias": "P1", "name": "Le Plateau"},
            {"alias": "P5", "name": "Les Granges"},
            {"alias": "P6", "name": "Peyrot"},
            {"alias": "P10", "name": "La Croix"},
        ],
        products=[
            {
                "name": COPPER,
                "active_substance": "copper",
                "product_type": "contact",
                "targets": ["downy"],
            },
            {
                "name": SULPHUR,
                "active_substance": "sulphur",
                "product_type": "contact",
                "targets": ["powdery"],
            },
        ],
        today=now_local.date(),
        now_local=now_local,
    )


def extract(transcript: str, now_local: datetime = TEN_AM) -> DraftOperation:
    return RegexExtractor().extract(make_request(transcript, now_local))


def test_english_and_french_demo_sentences_give_the_same_draft():
    en = extract("Sprayed P1 with copper and sulphur, 2 kg, this morning")
    fr = extract("Traité Le Plateau au cuivre et soufre, 2 kilos, ce matin")

    assert en == fr
    assert en == DraftOperation(
        type="spray",
        parcels=["P1"],
        products=[COPPER, SULPHUR],
        dose_value=2.0,
        dose_unit="kg/ha",
        ts_start=datetime(2024, 4, 10, 8, 0, tzinfo=PARIS),
        confidence=en.confidence,
        adapter="regex",
        notes=[],
    )


def test_spec_sentence_with_two_named_parcels():
    draft = extract("Sprayed Les Granges and Peyrot with copper and sulphur, 2 kilos, this morning")

    assert draft.parcels == ["P5", "P6"]
    assert draft.products == [COPPER, SULPHUR]
    assert (draft.dose_value, draft.dose_unit) == (2.0, "kg/ha")


def test_this_morning_is_clamped_to_an_earlier_now():
    six_am = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)

    draft = extract("Sprayed P1 with copper, 2 kg, this morning", now_local=six_am)

    assert draft.ts_start == six_am
    assert "time clamped to now" in draft.notes


def test_this_morning_at_ten_is_eight():
    assert extract("Traité P1 au cuivre ce matin").ts_start == datetime(
        2024, 4, 10, 8, 0, tzinfo=PARIS
    )


@pytest.mark.parametrize("word", ["yesterday", "hier"])
def test_yesterday_is_eight_the_day_before(word):
    assert extract(f"Sprayed P1 with copper {word}").ts_start == datetime(
        2024, 4, 9, 8, 0, tzinfo=PARIS
    )


def test_no_time_word_uses_now_with_a_note():
    draft = extract("Sprayed P1 with copper, 2 kg")

    assert draft.ts_start == TEN_AM
    assert "no time stated; using now" in draft.notes


def test_comma_lists_aliases_and_litres():
    draft = extract("Sprayed P1, P5 and P10 with sulfur, 1,5 L/ha this morning")

    assert draft.parcels == ["P1", "P5", "P10"]
    assert draft.products == [SULPHUR]
    assert (draft.dose_value, draft.dose_unit) == (1.5, "L/ha")


def test_unknown_alias_and_missing_substance_are_dropped_with_notes():
    req = make_request("Sprayed P1 and P7 with copper, 2 kg")
    req = req.model_copy(update={"products": [p for p in req.products if p.name == SULPHUR]})

    draft = RegexExtractor().extract(req)

    assert draft.parcels == ["P1"]
    assert draft.products == []
    assert "dropped unknown parcel: P7" in draft.notes
    assert "dropped unknown product: copper" in draft.notes


def test_a_shortlist_name_said_verbatim_is_kept():
    draft = extract("Sprayed Peyrot with Sulfo Jet 80 WG this morning")

    assert draft.parcels == ["P6"]
    assert draft.products == [SULPHUR]
    assert draft.dose_value is None


def test_a_distinctive_first_word_of_a_shortlist_name_picks_that_product():
    # What base.en heard for the spec sentence with brand names in its prompt.
    draft = extract("Sprayed like ranges and Peyrot with Copper and Sulfo, 2 kilos, this morning")

    assert draft.parcels == ["P6"]
    assert draft.products == [COPPER, SULPHUR]


def test_a_first_word_shared_by_two_shortlist_names_is_not_used():
    req = make_request("Sprayed P1 with Sulfo, 2 kg")
    extra = req.products[1].model_copy(update={"name": "Sulfo Max", "active_substance": "x"})
    req = req.model_copy(update={"products": [*req.products, extra]})

    assert RegexExtractor().extract(req).products == []


def test_any_shortlist_active_substance_said_picks_its_product():
    req = make_request("Traité P1 au cymoxanil, 0,2 kg")
    penetrant = req.products[0].model_copy(
        update={"name": "Cymo Plus", "active_substance": "cymoxanil", "product_type": "penetrant"}
    )
    req = req.model_copy(update={"products": [*req.products, penetrant]})

    assert RegexExtractor().extract(req).products == ["Cymo Plus"]


@pytest.mark.parametrize(
    "sentence, dose",
    [
        ("Sprayed P1 with copper, two kilos", (2.0, "kg/ha")),
        ("Traité P1 au cuivre, deux kilos", (2.0, "kg/ha")),
        ("Traité P1 au soufre, un litre", (1.0, "L/ha")),
        ("Sprayed P1 with sulphur, three litres per hectare", (3.0, "L/ha")),
    ],
)
def test_spelled_out_doses(sentence, dose):
    draft = extract(sentence)

    assert (draft.dose_value, draft.dose_unit) == dose


@pytest.mark.parametrize(
    "sentence",
    [
        "Sprayed P1 with copper at 6 this morning",
        "Traité P1 au cuivre à 6h ce matin",
        "Traité P1 au cuivre ce matin à 6 heures",
    ],
)
def test_an_explicit_clock_time_wins_over_the_morning_default(sentence):
    assert extract(sentence).ts_start == datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)


def test_clock_time_with_minutes_yesterday():
    draft = extract("Sprayed P1 with copper yesterday at 17:30")

    assert draft.ts_start == datetime(2024, 4, 9, 17, 30, tzinfo=PARIS)


def test_hyphenated_alias():
    assert extract("Sprayed P-1 with copper").parcels == ["P1"]
