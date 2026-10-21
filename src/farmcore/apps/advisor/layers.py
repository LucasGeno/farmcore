"""`GET /advisor/layer.geojson`: the "Spray advice" map layer (`MapLayer("advice", ...)`).

One feature per parcel (P1..P10), the parcels GeoJSON of the fields app plus what the map needs
to colour it: `label` and `colour` from `fields.service.DECISION_STYLES` ("No advice yet" for a
parcel with no advice up to the clock), `hatch` for spray, and the popup's cover end
(`protected_until`, local date, for a `protected` decision only: the sulphur-only pass can turn a
protected advice into spray/wait and keep the downy cover there, and the popup must not show a
cover date next to a Spray chip, as the advice card and Today do), and the parcel's priority
(`priority`, `priority_label`, `priority_why`, `priority_assumption`; `priority.py`). Every
read goes through the request's `Repo` and is bounded by `clock.now()`, so stepping the replay
clock back hides later advice.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from farmcore.apps.advisor import priority, service
from farmcore.apps.fields.service import (
    NOT_ADVISED_LABEL,
    UNCERTAIN_STYLE,
    parcels_feature_collection,
    styles_for,
)
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.web import context_url

router = APIRouter()


@router.get("/layer.geojson")
def advice_layer(request: Request, repo: Repo) -> JSONResponse:
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    zone = ZoneInfo(settings.display_tz)
    latest = service.latest_advice(repo, now, tz=settings.display_tz)
    collection = parcels_feature_collection(repo, now, tz=settings.display_tz, assessments=latest)
    for feature in collection["features"]:
        props = feature["properties"]
        label, colour = styles_for(request)[props["decision"]]
        advice = latest.get(props["id"])
        status = (advice.props or {}).get("assessment_status") if advice else None
        if status in ("limited", "unavailable"):
            props["decision"] = advice.decision
            if props["decision"] in ("spray", "wait"):
                # An observed risk stays actionable; say the assessment is limited.
                label = f"{label} (assessment {status})"
            else:
                label = "Assessment " + status
                colour = UNCERTAIN_STYLE[request.cookies.get("sunlight") == "1"]
        until = advice.protected_until if advice and props["decision"] == "protected" else None
        props.update(
            label=label if props["advised"] else NOT_ADVISED_LABEL,
            colour=colour,
            hatch=props["decision"] == "spray",
            protected_until=until.astimezone(zone).date().isoformat() if until else None,
            **priority.as_dict(priority.of_advice(advice)),
        )
        props["card_url"] = context_url(request, props["card_url"])  # keeps an explicit ?farm=
    return JSONResponse(collection, media_type="application/geo+json")
