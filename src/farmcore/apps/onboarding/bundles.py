"""App bundles by farm type: a static starting point, resolved through the registry.

A bundle names FarmCore apps; the registry adds their dependencies and switches the whole set
on atomically. A bundle never switches anything off: the owner can still change every app on
the Apps page afterwards. The apps each bundle names are a product choice, not an agronomic
rule.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bundle:
    key: str
    title: str
    summary: str
    apps: tuple[str, ...]
    icon: str = "leaf"


# Every bundle starts from the same working set (advice, weather and spray windows, planned
# jobs, stock and sharing); a farm type adds what it needs on top.
BASE = ("advisor", "stations", "forecast", "planning", "inventory", "share")

BUNDLES: tuple[Bundle, ...] = (
    Bundle(
        "vineyard",
        "Vineyard",
        "Spray windows, crop protection and stock for vine parcels.",
        BASE,
        icon="leaf",
    ),
    Bundle(
        "orchard",
        "Orchard",
        "Spray windows and crop protection, plus fertiliser applications for fruit trees.",
        (*BASE, "fertilization"),
        icon="leaf",
    ),
    Bundle(
        "arable",
        "Arable",
        "Spray windows, fertiliser applications and season impact for arable crops.",
        (*BASE, "fertilization", "kpi"),
        icon="tractor",
    ),
    Bundle(
        "mixed",
        "Mixed",
        "Everything for a farm with several enterprises.",
        (*BASE, "fertilization", "kpi", "review"),
        icon="apps",
    ),
)

BY_KEY = {bundle.key: bundle for bundle in BUNDLES}


def bundle(key: str) -> Bundle | None:
    return BY_KEY.get(key)


def matches(bundle: Bundle, app_titles: list[str], query: str) -> bool:
    """Whether the bundle is offered for a free-text search over titles and app names."""
    words = " ".join((bundle.title, bundle.summary, *app_titles)).casefold()
    return not query or query.casefold() in words
