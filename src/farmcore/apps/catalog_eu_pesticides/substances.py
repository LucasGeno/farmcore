"""The normalised substance key every country catalog shares.

The EU Pesticides Database, France's E-Phy and Italy's register each spell an active substance
their own way (case, surrounding whitespace, a parenthesised qualifier). A product from any
country links to an EU substance through a key resolved via `catalog_substance_alias`.

`substance_key(name)` is the canonical string form of a reference name: lowercased, trimmed,
whitespace collapsed. The key is name-based and best effort: it does not fold accents or
translate between languages. Qualifiers are handled by the alias map instead of the key itself,
because stripping them in the key would collide genuinely distinct EU entries (e.g. `Sodium
hydrogen carbonate (basic substance)` vs `(low risk …)`, `Carbon dioxide (active …)` vs
`(basic …)`).

E-Phy does publish CAS numbers, in a separate archive file (`substance_active_utf8.csv`), but the
product rows the importer reads carry only the reference name; a CAS join adds little over the
alias map, so it is not used yet. Its `Variant` French spellings are another later option.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
# A trailing parenthesised group that is a CAS number is part of the identity, not a qualifier.
_CAS = re.compile(r"\(cas[^)]*\)", re.IGNORECASE)
# `Boscalid (formerly nicobifen)`, `Warfarin (aka coumaphene)`.
_AKA = re.compile(r"\((aka|formerly)\s+([^)]*)\)", re.IGNORECASE)
_TRAILING = re.compile(r"\s*\(([^()]*)\)\s*$")


def substance_key(name: str) -> str:
    """The canonical key: lowercased, trimmed, internal whitespace collapsed to one space."""
    return _WS.sub(" ", name.strip().lower())


def strip_qualifiers(name: str) -> str:
    """The name with trailing parenthesised qualifiers removed, except a `(CAS …)` group."""
    stripped = name.strip()
    while True:
        match = _TRAILING.search(stripped)
        if match is None:
            break
        if _CAS.fullmatch(match.group(0).strip()):
            break
        stripped = stripped[: match.start()].strip()
    return stripped


def aliases_for(name: str) -> set[str]:
    """The keys a substance reference should resolve to: its exact key, the key with trailing
    parenthesised qualifiers stripped (except CAS), and each `(aka X)` / `(formerly X)` name."""
    result = {substance_key(name)}
    stripped = substance_key(strip_qualifiers(name))
    if stripped:
        result.add(stripped)
    for match in _AKA.finditer(name):
        alias = substance_key(match.group(2))
        if alias:
            result.add(alias)
    return {key for key in result if key}


def build_alias_map(
    names: list[str], product_names: tuple[str, ...] | list[str] = ()
) -> dict[str, str]:
    """`alias_key -> substance_key` for a set of EU substance names, dropping any alias that would
    resolve to more than one substance (ambiguous qualifiers such as `(basic substance)`)."""
    by_alias: dict[str, set[str]] = {}
    for name in names:
        key = substance_key(name)
        for alias in aliases_for(name):
            by_alias.setdefault(alias, set()).add(key)
    result = {alias: next(iter(keys)) for alias, keys in by_alias.items() if len(keys) == 1}
    # Preserve the product's exact key. Add its qualified spelling only when the EU lookup
    # resolves it unambiguously; never overwrite an exact EU identity or an ambiguous alias.
    eu_aliases = dict(result)
    for name in product_names:
        key = substance_key(name)
        if key in by_alias:
            continue
        targets = {eu_aliases[a] for a in aliases_for(name) if a in eu_aliases}
        if len(targets) == 1:
            result[key] = targets.pop()
    return result
