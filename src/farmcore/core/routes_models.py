"""`GET /models/{key}` and `GET /models?kind=`: "About this model", generated from its `ModelSpec`.

The page shows only what the spec declares (method, codes, BBCH window, inputs, each parameter
with its source and status, validation level, provenance), for the models of apps that are on.
The validation badge beside every assessment result links here (`core/_validation.html`).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from farmcore.core.model_spec import collect_model_specs, find_spec
from farmcore.core.registry import visible_manifests
from farmcore.web import render

router = APIRouter()


@router.get("/models", response_class=HTMLResponse)
def models(request: Request, kind: str = "") -> HTMLResponse:
    by_kind = collect_model_specs(visible_manifests(request))
    if kind:
        if kind not in by_kind:
            raise HTTPException(404, "No such assessment kind")
        specs = by_kind[kind]
    else:
        specs = tuple({s.key: s for group in by_kind.values() for s in group}.values())
    return render(request, "core/models.html", specs=specs)


@router.get("/models/{key}", response_class=HTMLResponse)
def model(request: Request, key: str) -> HTMLResponse:
    spec = find_spec(visible_manifests(request), key)
    if spec is None:
        raise HTTPException(404, "No such model")
    return render(request, "core/models.html", specs=(spec,))
