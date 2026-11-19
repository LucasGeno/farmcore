"""Manifest of app `fertilization`: record what fertiliser went on which parcel this season.

Optional. Applications are drafts the owner confirms (`records.py`); confirmed ones are
`fertilise` logs in the diary, on the parcel's map panel and in the assistant's answers, and a
fertiliser with a stock row is counted by Inventory. No rate recommendation, no filing.
"""

from farmcore.apps.fertilization.tools import TOOLS
from farmcore.core.manifest import DraftKind, Manifest, RecordType

_RECORDS = "farmcore.apps.fertilization.records"
_REVIEW = "/fertilization/drafts/review"

MANIFEST = Manifest(
    name="fertilization",
    version="0.1.0",
    summary="What fertiliser went on which parcel this season, with its nutrients.",
    # inventory: the fertiliser's stock unit (density asked) and its season (`season_start`).
    depends=("core", "fields", "operations", "inventory"),
    # logs:read: the records; fields:read: parcel names and areas; corrections:read: receipts.
    scopes=("logs:read", "fields:read", "corrections:read"),
    routes=("farmcore.apps.fertilization.routes:router",),
    title="Fertilization",
    category="Essentials",
    nav=("Fertilization", "/fertilization/"),
    nav_group="apps",
    nav_order=30,
    nav_icon="leaf",
    icon="leaf",
    color="brand",
    keywords={
        "en": ("fertilization", "fertiliser", "fertilizer", "manure", "nitrogen"),
        "fr": ("fertilisation", "engrais", "fumure", "azote"),
        "es": ("fertilización", "abonado", "fertilizante", "nitrógeno"),
        "it": ("fertilizzazione", "concime", "concimazione", "azoto"),
    },
    record_types=(
        RecordType("fertilization", "Fertilization", "leaf", "/fertilization/new", "fertilization"),
    ),
    draft_kinds=(
        DraftKind("fertilization", f"{_RECORDS}:validate", _REVIEW, f"{_RECORDS}:confirm"),
        DraftKind(
            "fertilization-correction",
            f"{_RECORDS}:validate_correction",
            _REVIEW,
            f"{_RECORDS}:confirm_correction",
        ),
    ),
    parcel_sections=("/fertilization/map/parcels/{id}",),
    tools=TOOLS,
    maturity="alpha",
)
