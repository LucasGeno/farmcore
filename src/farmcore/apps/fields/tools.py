"""Fields assistant tools (read-only): `fields_list_parcels`, and the parcel resolution every
parcel tool shares.

`resolve_parcel` matches an alias or a name, ignoring case, accents and punctuation ("bel air" ~
"Bel-Air"). It has its own fold: fields must not import chat.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from farmcore.apps.fields.seed import alias_number
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Parcel
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec

SCOPE = "fields:read"
UNKNOWN_PARCEL = "unknown parcel"
LIST_FACTS = frozenset({"parcels", "alias", "name", "variety", "area_ha"})
ERROR_FACTS = frozenset({"error", "parcels"})

TOOLS = (
    ToolSpec(
        name="fields_list_parcels",
        description="The farm's vine parcels: alias, name, grape variety and area in hectares.",
        params={},
        func="farmcore.apps.fields.tools:list_parcels",
        scope=SCOPE,
        keywords=("my parcels", "list parcels", "parcels", "mes parcelles", "parcelles"),
    ),
)


def fold(text: str) -> str:
    """Lower case, no accents, punctuation as single spaces: "Bel-Air" -> "bel air"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


def parcels(repo: ScopedRepository) -> list[Parcel]:
    """The farm's parcels, P1..P10."""
    rows = repo.list(Parcel, scope=SCOPE)
    return sorted(rows, key=lambda p: (alias_number(p.alias), p.alias))


def label(parcel: Parcel) -> str:
    return f"{parcel.alias} {parcel.name}"


def _match(said: str, rows: list[Parcel]) -> Parcel | None:
    key = fold(said)
    for p in rows:
        if key in (fold(p.alias), fold(p.name), fold(label(p))):
            return p
    return None


def resolve_parcel(repo: ScopedRepository, said: str) -> Parcel | None:
    """The parcel whose alias or name is `said`, or None."""
    return _match(said, parcels(repo)) if said.strip() else None


def mentioned_parcel(repo: ScopedRepository, text: str) -> Parcel | None:
    """The parcel whose alias or name appears as whole words in `text` (the longest match)."""
    padded = f" {fold(text)} "
    best: tuple[int, Parcel] | None = None
    for p in parcels(repo):
        for key in (fold(p.alias), fold(p.name)):
            if key and f" {key} " in padded and (best is None or len(key) > best[0]):
                best = (len(key), p)
    return best[1] if best else None


def unknown_parcel(ctx: ToolContext) -> ToolResult:
    """The error result every parcel tool returns for a parcel it cannot resolve."""
    names = [label(p) for p in parcels(ctx.repo)]
    if ctx.lang == "fr":
        text = "Je ne connais pas cette parcelle. Vos parcelles : " + ", ".join(names) + "."
    else:
        text = "I don't know that parcel. Your parcels: " + ", ".join(names) + "."
    return ToolResult(facts={"error": UNKNOWN_PARCEL, "parcels": names}, text=text)


def list_parcels(ctx: ToolContext) -> ToolResult:
    rows = parcels(ctx.repo)
    facts: dict[str, Any] = {
        "parcels": [
            {
                "alias": p.alias,
                "name": p.name,
                "variety": (p.props or {}).get("variety"),
                "area_ha": float(p.area_ha),
            }
            for p in rows
        ]
    }
    names = ", ".join(label(p) for p in rows)
    if ctx.lang == "fr":
        text = f"{len(rows)} parcelles : {names}."
    else:
        text = f"{len(rows)} parcels: {names}."
    return ToolResult(facts=facts, text=text, links=(("Open map", "/fields/"),))
