"""The agronomic model contract: what a model is, where its numbers come from, how far to trust it.

Every model behind an assessment declares a `ModelSpec`: its method and citation, EPPO host and
pest codes, the BBCH window it runs in, the inputs it reads, each parameter with its source and
status, its validation level and its provenance. An `AssessmentKind` names the specs of the
models that compute it (`AssessmentKind.models`), so the validation level travels with every
stored result of that kind and the UI can show it beside the result (`kind_validation`).

A spec is checked when it is built and again when the registry loads the apps
(`collect_model_specs`, from `registry.resolve_order`): a missing declaration, a parameter with
no citation that is not marked `unsourced`, a validation level it does not back up, or an
assessment kind with no model fails the start with `ModelSpecInvalid` naming the model and
what is missing. Nothing is inferred: a citation that is not known is never filled in.

The module is named `model_spec`, not `models_contract`: `core/models/` is the ORM package, and
these are agronomic models, not tables.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from farmcore.core.manifest import AssessmentKind, import_string

# M2 validation levels, weakest first. Every level above `unvalidated` must say what backs it.
VALIDATION_LEVELS: dict[str, str] = {
    "unvalidated": "Unvalidated",
    "literature-evaluated": "Literature-evaluated",
    "locally-evaluated": "Locally evaluated",
    "official-service": "Used by an official service",
}
PARAMETER_STATUSES = ("sourced", "assumed", "unsourced")
INPUT_ORIGINS = ("measured", "estimated")

_KEY = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
# EPPO codes: 5 or 6 upper-case letters/digits (VITVI, PLASVI, UNCINE).
_EPPO = re.compile(r"[A-Z0-9]{5,6}")


class ModelSpecInvalid(ValueError):
    """A model or assessment kind whose declaration is missing or does not hold."""


@dataclass(frozen=True)
class Method:
    """The published method: its name, the citation it is implemented from, and DOI if known."""

    name: str = ""
    citation: str = ""
    doi: str = ""


@dataclass(frozen=True)
class Stages:
    """The BBCH window the model runs in; `end` None when the model has no end stage."""

    start: int | None = None
    end: int | None = None
    note: str = ""


@dataclass(frozen=True)
class ModelInput:
    variable: str
    unit: str
    resolution: str
    origin: str  # measured | estimated


@dataclass(frozen=True)
class Parameter:
    """One value the model uses. `source` cites it (with `doi` and `page` when known); a value
    whose source is not known is `status="unsourced"` and has no source."""

    name: str
    value: Any
    unit: str = ""
    source: str = ""
    status: str = "sourced"  # sourced | assumed | unsourced
    doi: str = ""
    page: str = ""
    region: str = ""
    note: str = ""


@dataclass(frozen=True)
class Validation:
    """The M2 level and what backs it: region and years, metrics, or the service's name."""

    level: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return VALIDATION_LEVELS.get(self.level, self.level)


@dataclass(frozen=True)
class ModelSpec:
    key: str  # <app>.<model>
    title: str = ""
    method: Method = field(default_factory=Method)
    host: str = ""  # EPPO code of the crop
    pests: tuple[str, ...] = ()  # EPPO codes of the pests or diseases
    stages: Stages = field(default_factory=Stages)
    inputs: tuple[ModelInput, ...] = ()
    parameters: tuple[Parameter, ...] = ()
    validation: Validation = field(default_factory=Validation)
    provenance: str = ""

    def __post_init__(self) -> None:
        problems = list(_problems(self))
        if problems:
            raise ModelSpecInvalid(f"model {self.key!r}: {'; '.join(problems)}")


def _problems(spec: ModelSpec) -> Iterable[str]:
    if not _KEY.fullmatch(spec.key or ""):
        yield "key must be '<app>.<model>'"
    if not spec.title:
        yield "missing title"
    if not spec.method.name or not spec.method.citation:
        yield "missing method (name and citation)"
    if not _EPPO.fullmatch(spec.host):
        yield "missing host EPPO code"
    if not spec.pests or not all(_EPPO.fullmatch(p) for p in spec.pests):
        yield "missing pest EPPO codes"
    start, end = spec.stages.start, spec.stages.end
    if start is None or not 0 <= start <= 99 or (end is not None and not start <= end <= 99):
        yield "missing BBCH stages (0-99, start <= end)"
    if not spec.inputs:
        yield "missing inputs"
    for i in spec.inputs:
        if not (i.variable and i.unit and i.resolution) or i.origin not in INPUT_ORIGINS:
            yield f"input {i.variable!r} needs a unit, a resolution and measured|estimated"
    if not spec.parameters:
        yield "missing parameters"
    for p in spec.parameters:
        if p.status not in PARAMETER_STATUSES:
            yield f"parameter {p.name!r}: status must be one of {', '.join(PARAMETER_STATUSES)}"
        elif p.status == "unsourced" and (p.source or p.doi):
            yield f"parameter {p.name!r} is unsourced but names a source"
        elif p.status != "unsourced" and not p.source:
            yield f"parameter {p.name!r} has no source: cite one or mark it 'unsourced'"
    if spec.validation.level not in VALIDATION_LEVELS:
        yield f"missing validation level ({', '.join(VALIDATION_LEVELS)})"
    elif spec.validation.level != "unvalidated" and not spec.validation.detail:
        yield f"validation {spec.validation.level!r} needs what backs it (region, years, metrics)"
    if not spec.provenance:
        yield "missing provenance"


def models_of(kind: AssessmentKind) -> tuple[ModelSpec, ...]:
    """The specs `kind` names; `ModelSpecInvalid` when it names none or a path is not a spec."""
    if not kind.models:
        raise ModelSpecInvalid(
            f"assessment kind {kind.key!r} declares no model: name its ModelSpec in "
            "AssessmentKind.models"
        )
    specs = tuple(import_string(path) for path in kind.models)
    for path, spec in zip(kind.models, specs, strict=True):
        if not isinstance(spec, ModelSpec):
            raise ModelSpecInvalid(f"assessment kind {kind.key!r}: {path!r} is not a ModelSpec")
    return specs


def collect_model_specs(manifests) -> dict[str, tuple[ModelSpec, ...]]:
    """Assessment kind key -> the specs of its models, for every kind the manifests declare.

    Runs at load: a kind with no model, a path that is not a `ModelSpec`, or two different specs
    under one model key raise `ModelSpecInvalid`."""
    by_kind: dict[str, tuple[ModelSpec, ...]] = {}
    seen: dict[str, ModelSpec] = {}
    for m in manifests:
        for kind in m.assessment_kinds:
            by_kind[kind.key] = models_of(kind)
            for spec in by_kind[kind.key]:
                if seen.setdefault(spec.key, spec) is not spec:
                    raise ModelSpecInvalid(f"model {spec.key!r} is declared twice")
    return by_kind


@dataclass(frozen=True)
class KindValidation:
    """What a result of one assessment kind can claim: its weakest model's level."""

    kind: str
    validation: Validation
    models: tuple[ModelSpec, ...]


def _weakest(kind: str, specs: tuple[ModelSpec, ...]) -> KindValidation:
    order = list(VALIDATION_LEVELS)
    weakest = min(specs, key=lambda s: order.index(s.validation.level))
    return KindValidation(kind, weakest.validation, specs)


def kind_validation(manifests, kind: str) -> KindValidation | None:
    """The validation shown beside a result of `kind`; None when no loaded app declares it."""
    specs = collect_model_specs(manifests).get(kind)
    return _weakest(kind, specs) if specs else None


def validation_json(kind: AssessmentKind) -> dict[str, Any]:
    """What a result of `kind` carries in JSON: the weakest level, its models and their About."""
    v = models_of(kind)
    summary = _weakest(kind.key, v)
    return {
        "level": summary.validation.level,
        "label": summary.validation.label,
        "detail": summary.validation.detail,
        "models": [s.key for s in v],
        "about": f"/models?kind={kind.key}",
    }


def find_spec(manifests, key: str) -> ModelSpec | None:
    for specs in collect_model_specs(manifests).values():
        for spec in specs:
            if spec.key == key:
                return spec
    return None
