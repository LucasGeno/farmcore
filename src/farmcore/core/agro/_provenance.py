"""Where a value comes from: a citation, or an explicit assumption label."""

from __future__ import annotations

import re

ASSUMPTION = "assumption:"
# An identifiable citation: a URL with a host and a path, a DOI (10.<digits>/<suffix>), or an
# author-year reference (a word of 2+ letters and a 4-digit year). "2024", "DOI:" or "https://"
# alone identify nothing.
_URL = re.compile(r"https?://[^\s/]+\.[^\s/]+/\S+", re.IGNORECASE)
_DOI = re.compile(r"\b10\.\d+/\S+")
_WORD = re.compile(r"\b[^\W\d_]{2,}\b")
_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")


def _cites(text: str) -> bool:
    # What claims to be a DOI or a link must be one; it never passes as author-year instead.
    if text.lower().startswith("doi"):
        return bool(_DOI.search(text))
    if "://" in text:
        return bool(_URL.search(text))
    return bool(_DOI.search(text) or (_WORD.search(text) and _YEAR.search(text)))


def provenance_problem(source: str, status: str) -> str | None:
    """Why `source` does not back a value of `status` ("sourced" or "assumed"); None if it does.

    An assumed value carries an `assumption: <what is assumed>` label; a sourced one an identifiable
    citation (URL with host and path, DOI, or author and year) and no assumption label."""
    text = (source or "").strip()
    label = text.lower().startswith(ASSUMPTION)
    if status == "assumed":
        if not label or not text[len(ASSUMPTION) :].strip():
            return "an assumed value needs an 'assumption: ...' label saying what is assumed"
        return None
    if status == "sourced":
        if label or not _cites(text):
            return "a sourced value needs a citation: a URL, a DOI or an author and year"
        return None
    return f"status must be sourced or assumed, got {status!r}"
