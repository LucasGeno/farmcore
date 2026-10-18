"""Fields assistant tools on the seeded Scene B farm: `fields_list_parcels`, parcel resolution
(alias, name, case and accents) and the unknown-parcel error fact."""

from __future__ import annotations

import json

import pytest

from farmcore.apps.fields import tools
from tests.apps.advisor import scenes
from tests.apps.chat.assistant.toolkit import CHAT, assistant_reads, ctx_at, keys, write_counts

pytestmark = pytest.mark.data

manifests, seeded = scenes.manifests, scenes.seeded

NOW = "2024-04-10 06:00"
# The facts the model chooser may send, from the tool contract plus the "parcels"
# wrapper of the list. Literal on purpose: widening the allowlist is a test change.
LIST_FACTS = {"parcels", "alias", "name", "variety", "area_ha"}
# The unknown-parcel error fact : {"error": "unknown parcel", "parcels": [...]}.
ERROR_FACTS = {"error", "parcels"}
NAMES = [
    "P1 Le Plateau",
    "P2 La Croix",
    "P3 Les Sables",
    "P4 Le Tertre",
    "P5 Les Granges",
    "P6 Peyrot",
    "P7 La Fontaine",
    "P8 Le Moulin",
    "P9 Les Graves",
    "P10 Bel-Air",
]


def test_list_parcels_gives_alias_name_variety_and_area_in_alias_order(
    session, settings, seeded, data_dir
):
    seeded("B")
    ctx = ctx_at(session, settings, NOW)
    by_alias = {
        f["properties"]["alias"]: f["properties"]
        for f in json.loads((data_dir / "ign" / "demo_parcels_named.geojson").read_text())[
            "features"
        ]
    }

    result = tools.list_parcels(ctx)

    parcels = result.facts["parcels"]
    assert [f"{p['alias']} {p['name']}" for p in parcels] == NAMES
    assert parcels[5] == {
        "alias": "P6",
        "name": "Peyrot",
        "variety": "Cabernet Franc",
        "area_ha": pytest.approx(by_alias["P6"]["area_ha"]),
    }
    assert keys(result.facts) <= LIST_FACTS
    assert result.text.startswith("10 parcels: P1 Le Plateau, P2 La Croix,")
    assert len(result.text.splitlines()) <= 3


@pytest.mark.parametrize(
    ("said", "alias"),
    [
        ("P6", "P6"),
        ("p6", "P6"),
        ("Peyrot", "P6"),
        ("PEYROT", "P6"),
        ("Péyrot", "P6"),
        ("le plateau", "P1"),
        ("Le Platéau", "P1"),
        ("bel air", "P10"),
        ("P10", "P10"),
    ],
)
def test_resolve_parcel_by_alias_or_name_ignoring_case_and_accents(
    session, settings, seeded, said, alias
):
    seeded("B")
    ctx = ctx_at(session, settings, NOW)

    parcel = tools.resolve_parcel(ctx.repo, said)

    assert parcel is not None and parcel.alias == alias


def test_resolve_parcel_unknown_is_none(session, settings, seeded):
    seeded("B")
    ctx = ctx_at(session, settings, NOW)

    assert tools.resolve_parcel(ctx.repo, "Le Château") is None
    assert tools.resolve_parcel(ctx.repo, "P1") is not None


@pytest.mark.parametrize(
    ("text", "alias"),
    [
        ("When did I last spray Le Plateau?", "P1"),
        ("Is Peyrot protected?", "P6"),
        ("is p10 protected", "P10"),
        ("Est-ce que Les Granges est protégé ?", "P5"),
        ("What do I spray today?", None),
    ],
)
def test_mentioned_parcel_finds_a_parcel_named_in_a_question(
    session, settings, seeded, text, alias
):
    seeded("B")
    ctx = ctx_at(session, settings, NOW)

    parcel = tools.mentioned_parcel(ctx.repo, text)

    assert (parcel.alias if parcel else None) == alias


def test_unknown_parcel_result_lists_the_parcels(session, settings, seeded):
    seeded("B")
    ctx = ctx_at(session, settings, NOW)

    result = tools.unknown_parcel(ctx)

    assert result.facts == {"error": "unknown parcel", "parcels": NAMES}
    assert keys(result.facts) <= ERROR_FACTS
    assert "P6 Peyrot" in result.text


def test_list_parcels_reads_as_the_chat_with_purpose_assistant_and_writes_nothing(
    session, settings, seeded
):
    seeded("B")
    before = write_counts(session)
    ctx = ctx_at(session, settings, NOW)

    tools.list_parcels(ctx)

    assert (f"chat:{CHAT}", "parcel", "fields:read") in assistant_reads(session)
    assert write_counts(session) == before


def test_tools_are_declared_for_the_assistant():
    [spec] = tools.TOOLS
    assert spec.name == "fields_list_parcels"
    assert spec.scope == "fields:read"
    assert spec.func == "farmcore.apps.fields.tools:list_parcels"
