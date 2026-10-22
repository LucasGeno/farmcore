"""Advisor debug panel, mounted at `/advisor`: `GET /advisor/debug/parcels/{id}`.

A fragment for the core debug drawer (`debug_panels` URL, `{id}` = the page parcel id): the raw
advice JSON and, per reason, its rule code, raw inputs, thresholds (assumed ones marked) and
source. Admin only (403 otherwise); reads like the card (`advice:read` through the request's
`Repo`, bounded by `clock.now()`) and writes nothing but the reads' access_log rows.

It reads through `advisor/service.py` and never imports `advisor/routes.py`, whose card view
models the card rewrites.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.advisor import service
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import Parcel
from farmcore.core.routes_clock import require_admin
from farmcore.web import render

router = APIRouter()

PARCEL_SCOPE = "fields:read"
NO_ADVICE_HEADLINE = "No advice yet"
# The engine marks assumed thresholds in the reason note: "... (assumed: a, b)".
_ASSUMED_RE = re.compile(r"\(assumed: ([^)]*)\)")


def _raw(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _reason(reason: dict[str, Any]) -> dict[str, Any]:
    note = reason.get("note", "")
    match = _ASSUMED_RE.search(note)
    assumed = {n.strip() for n in match.group(1).split(",")} if match else set()
    return {
        "rule": reason.get("rule", ""),
        "verdict": reason.get("verdict", ""),
        "source": reason.get("source", ""),
        "note": note,
        "inputs": [(k, _raw(v)) for k, v in (reason.get("inputs") or {}).items()],
        "threshold": [
            (k, _raw(v), k in assumed) for k, v in (reason.get("threshold") or {}).items()
        ],
    }


@router.get(
    "/debug/parcels/{parcel_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def parcel_panel(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    parcel = repo.get(Parcel, parcel_id, scope=PARCEL_SCOPE)
    if parcel is None:
        raise HTTPException(status_code=404, detail=f"parcel {parcel_id} not found")
    now = get_clock(repo.session, request.app.state.settings).now()
    row = service.latest_advice(
        repo, now, tz=request.app.state.settings.display_tz, parcel_id=parcel.id
    ).get(parcel.id)
    body = (
        service.advice_json(row)
        if row is not None
        else service.empty_advice_json(parcel.id, NO_ADVICE_HEADLINE)
    )
    # The same parcel block as the card's `/advisor/parcels/{id}.json`.
    advice = {
        **body,
        "parcel": {
            "id": parcel.id,
            "alias": parcel.alias,
            "name": parcel.name,
            "area_ha": float(parcel.area_ha),
            "variety": (parcel.props or {}).get("variety"),
        },
    }
    return render(
        request,
        "advisor/_debug_panel.html",
        advice=advice,
        reasons=[_reason(r) for r in advice.get("reasons", [])],
        raw=json.dumps(advice, ensure_ascii=False, indent=2, default=str),
    )
