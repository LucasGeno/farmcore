"""Where a value comes from: a citation, or an explicit assumption label."""

from __future__ import annotations

import re

ASSUMPTION = "assumption:"
# A citation names its year, a DOI or a link; a bare word ("x", "table") says nothing.
_CITATION = re.compile(r"\b(1[89]|20)\d{2}\b|\bdoi\b|10\.\d{4,}/|https?://", re.IGNORECASE)


def provenance_problem(source: str, status: str) -> str | None:
    """Why `source` does not back a value of `status` ("sourced" or "assumed"); None if it does.

    An assumed value carries an `assumption: <what is assumed>` label; a sourced one a citation
    with a year, DOI or link, and no assumption label."""
    text = (source or "").strip()
    label = text.lower().startswith(ASSUMPTION)
    if status == "assumed":
        if not label or not text[len(ASSUMPTION) :].strip():
            return "an assumed value needs an 'assumption: ...' label saying what is assumed"
        return None
    if status == "sourced":
        if label or not _CITATION.search(text):
            return "a sourced value needs a citation with a year, DOI or link"
        return None
    return f"status must be sourced or assumed, got {status!r}"
