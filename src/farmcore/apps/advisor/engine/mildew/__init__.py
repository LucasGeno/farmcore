"""The mildew models as ModelSpec modules: one module per model owns its parameters.

`downy_3_10`, `downy_incubation` and `powdery_gubler_thomas` each declare `SPEC` (its
`ModelSpec`) and the `Parameter`s it is built from. `engine/config.py` reads its defaults from
these parameters (`AdvisorConfig`), so a rule and its spec cannot disagree. Season dates are set
per region and year in `config.py`; their spec value is None.

No citation is added here that the rules did not already carry; a DOI is attached only to a
source `DOIS` names (verified against the publisher), and left empty otherwise. Validation is
honest: the models have only been replayed on one farm's seasons against a calendar programme,
so every model is `unvalidated`.
"""

from __future__ import annotations

from typing import Any

from farmcore.core.model_spec import ModelInput, Parameter, Validation

PENDING = "assumed: awaiting agronomist"

HOST = "VITVI"  # Vitis vinifera
DOWNY = "PLASVI"  # Plasmopara viticola
POWDERY = "UNCINE"  # Erysiphe necator

REPLAYED = (
    "replayed on the 2024 and 2025 seasons of one farm against a calendar programme; "
    "not yet checked against observed disease records or other regions"
)
VALIDATION = Validation("unvalidated", REPLAYED)
PROVENANCE = (
    "Implemented in FarmCore from the published rules cited per parameter; no third-party model "
    "code. No golden fixtures from other implementations yet."
)
# Known DOIs, by a phrase of the source that cites them.
DOIS = {
    "Velasquez-Camacho et al. 2023": "10.3390/microorganisms11010073",
    "Pérez-Expósito et al. 2017": "10.3390/s17030465",
    "Gadoury 2003": "10.1094/PHYTO.2003.93.5.547",
    "Racković et al. 2026": "10.3390/s26092843",
    "Parton & Logan 1981": "10.1016/0002-1571(81)90105-9",
    "Rossi et al. 2008": "10.1016/j.ecolmodel.2007.10.046",
}


def doi(source: str) -> str:
    return next((d for phrase, d in DOIS.items() if phrase in source), "")


def param(
    name: str, value: Any, source: str, unit: str = "", *, assumed: bool = False
) -> Parameter:
    """A sourced (or `assumed`) parameter; its DOI comes from `DOIS`."""
    return Parameter(
        name=name,
        value=value,
        unit=unit,
        source=source,
        doi=doi(source),
        status="assumed" if assumed else "sourced",
    )


def dated(name: str, source: str) -> Parameter:
    """A date set per region and year in `config.py`: no value here, `source` is the default
    region's."""
    return Parameter(
        name=name,
        value=None,
        unit="date",
        source=source,
        doi=doi(source),
        status="assumed",
        region="per region and year",
    )


def hourly(variable: str, unit: str, origin: str = "measured") -> ModelInput:
    return ModelInput(variable, unit, "hourly", origin)


def by_name(parameters: tuple[Parameter, ...]) -> dict[str, Parameter]:
    return {p.name: p for p in parameters}
