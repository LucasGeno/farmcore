"""The normalised substance key every country catalog shares.

The EU Pesticides Database, France's E-Phy and Italy's register each spell an active substance
their own way (case, surrounding whitespace). A product from any country links to an EU substance
through `substance_key(name)`, a canonical string form of the substance's reference name.

The key is name-based and best effort: it lowercases, trims and collapses whitespace. It does not
fold accents, expand abbreviations or translate between languages, and it does not use a CAS
number (E-Phy and the Italian register publish none). A name that differs across sources for that
reason (e.g. EU "Sulphur" vs a French "Soufre") resolves to different keys; that is a known limit,
not a silent wrong link.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def substance_key(name: str) -> str:
    """The canonical key: lowercased, trimmed, internal whitespace collapsed to one space."""
    return _WS.sub(" ", name.strip().lower())
