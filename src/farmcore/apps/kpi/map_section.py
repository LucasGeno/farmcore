"""`GET /kpi/map/farm`: Farm today's one link to Season & impact, mounted at `/kpi`.

No figures: the season KPIs live on `/kpi/` with their assumptions. The request's `Repo` resolves
the selected farm (`?farm=` before the cookie) as on every app route; the link keeps the farm
query. A plain HTML fragment (a `<section>`, no page chrome).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farmcore.core.access import Repo
from farmcore.web import render

router = APIRouter()

HOME_PATH = "/kpi/"


@router.get("/map/farm", response_class=HTMLResponse)
def farm_section(request: Request, repo: Repo) -> HTMLResponse:
    del repo  # resolves and authorizes the selected farm; nothing is read
    return render(request, "kpi/_map_farm.html", home=HOME_PATH)
