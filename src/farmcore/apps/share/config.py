"""Share app constants. Every value names its source; routes read them from here."""

from __future__ import annotations

from types import MappingProxyType

# Grant presets: key -> (label, scopes). Ordered; the first is the form default.
# (demo step: "grant the co-op adviser read access to spray logs only"). No preset offers a
# write scope, the audit scope or `*`: a third party never writes and never reads the audit.
PRESETS: MappingProxyType[str, tuple[str, tuple[str, ...]]] = MappingProxyType(
    {
        "adviser_logs": (
            "Co-op adviser — spray logs and register (read only)",
            ("logs:read", "register:read"),
        ),
        "station": ("Weather service — weather station data (read only)", ("stations:read",)),
        "advice": ("Advice reviewer — stored advice only (read only)", ("advice:read",)),
    }
)
DEFAULT_PRESET = next(iter(PRESETS))

# Form defaults.
DEFAULT_GRANTEE = "Co-op adviser"
DEFAULT_PURPOSE = "spray advice"
DEFAULT_VALID_DAYS = 30  # a season-sized consent; the farmer can revoke earlier
GRANTEE_MAX_LEN = 80  # a person's or an organisation's name, not free text
PURPOSE_MAX_LEN = 200  # one line of purpose, shown in the access log
ACCESS_LOG_LIMIT = 50  # rows shown on /share/ (one screen of reads)

# Plain words per scope (`access.SCOPES`), for the grants table and the grantee banner.
SCOPE_WORDS: MappingProxyType[str, str] = MappingProxyType(
    {
        "fields:read": "parcels",
        "stations:read": "weather station data",
        "logs:read": "spray logs",
        "logs:write": "writing spray logs",
        "advice:read": "spray advice",
        "register:read": "spray register",
        "audit:read": "access and model-call audit",
    }
)

# NGSI-LD @context of the smart-data-models Agrifood subject (as in AgriParcelOperation's
# published examples).
NGSI_CONTEXT = (
    "https://raw.githubusercontent.com/smart-data-models/dataModel.Agrifood/master/context.jsonld"
)
# farmcore `log.type` -> AgriParcelOperation `operationType`. Source: the schema.json enum
# (fertiliser, inspection, pesticide, water, other); only sprays are exported.
OPERATION_TYPES: MappingProxyType[str, str] = MappingProxyType({"spray": "pesticide"})
# farmcore quantity unit -> UN/CEFACT Recommendation No. 20 common code. Only codes checked
# against the Rec 20 code list (UNECE, via the datasets/unece-units-of-measure CSV mirror):
# KGM kilogram, GRM gram, LTR litre, MLT millilitre, TNE tonne. Rec 20 has no kilogram- or
# litre-per-hectare code, so `kg/ha` and `L/ha` export no `unitCode` (the unit text goes in
# `description`). Never add a code that was not checked.
UNIT_CODES: MappingProxyType[str, str] = MappingProxyType(
    {
        "kg": "KGM",
        "g": "GRM",
        "L": "LTR",
        "l": "LTR",
        "mL": "MLT",
        "ml": "MLT",
        "t": "TNE",
    }
)
