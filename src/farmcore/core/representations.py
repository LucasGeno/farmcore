"""Versioned public representations: what a published JSON shape is called and which versions of
it a client may ask for.

A representation another system consumes (the NGSI-LD export, shared advice) keeps its meaning
when the data under it moves: a change that would alter a published key or id ships as a new
version beside the old one, and the old one is deprecated with a date before it is removed
(`docs/api-compatibility.md`). A client picks a version with `?v=<n>`; without it, it gets the
default. Every response names what it is in `FarmCore-Representation: <name>; v=<n>`, and a
deprecated version also sends `Deprecation` and `Sunset` (RFC 9745, RFC 8594) with a link to the
notes.

Core holds the registry only; the app that renders a representation registers it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import format_datetime

from fastapi import HTTPException, Request, Response

HEADER = "FarmCore-Representation"
QUERY = "v"
NOTES = "https://github.com/LucasGeno/farmcore/blob/main/docs/api-compatibility.md"


@dataclass(frozen=True)
class Version:
    number: int
    deprecated: date | None = None  # deprecated since; None: current
    sunset: date | None = None  # last day it is served; None: no date yet
    note: str = ""


@dataclass(frozen=True)
class Representation:
    name: str
    versions: tuple[Version, ...]
    default: int
    by_number: dict[int, Version] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        numbers = [v.number for v in self.versions]
        if not numbers or len(set(numbers)) != len(numbers) or min(numbers) < 1:
            raise ValueError(f"representation {self.name!r}: versions must be distinct, from 1")
        if self.default not in numbers:
            raise ValueError(f"representation {self.name!r}: default {self.default} is unknown")
        object.__setattr__(self, "by_number", {v.number: v for v in self.versions})


REGISTRY: dict[str, Representation] = {}


def register(rep: Representation) -> Representation:
    """Register `rep` once; registering a different definition under the same name raises."""
    held = REGISTRY.setdefault(rep.name, rep)
    if held != rep:
        raise ValueError(f"representation {rep.name!r} is already registered differently")
    return held


def negotiate(request: Request, name: str) -> Version:
    """The version this request asked for (`?v=<n>`), else the default; an unknown or malformed
    version is a 400 naming the ones served, never a silent fallback."""
    rep = REGISTRY[name]
    raw = request.query_params.get(QUERY)
    if raw is None:
        return rep.by_number[rep.default]
    # Matched as text against the served numbers: no int() of a client string (a 5000-digit
    # value would raise), and no non-ASCII digit read as a version.
    served_text = {str(n): v for n, v in rep.by_number.items()}
    if raw in served_text:
        return served_text[raw]
    served = ", ".join(str(n) for n in sorted(rep.by_number))
    raise HTTPException(400, f"Unknown {name} version {raw!r}; served versions: {served}.")


def stamp[R: Response](response: R, name: str, version: Version) -> R:
    """Name the representation on the response, with deprecation headers when it is old."""
    response.headers[HEADER] = f"{name}; v={version.number}"
    if version.deprecated is not None:
        since = datetime.combine(version.deprecated, datetime.min.time(), UTC)
        response.headers["Deprecation"] = f"@{int(since.timestamp())}"
        response.headers["Link"] = f'<{NOTES}>; rel="deprecation"'
    if version.sunset is not None:
        end = datetime.combine(version.sunset, datetime.max.time(), UTC).replace(microsecond=0)
        response.headers["Sunset"] = format_datetime(end, usegmt=True)
    return response
