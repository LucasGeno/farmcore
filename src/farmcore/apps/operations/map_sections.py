"""Map panel sections of `operations`, mounted at `/operations`.

- `GET /operations/map/farm`: Farm today's latest confirmed spray up to the clock: its date,
  parcels and products as the register lists them for that log (one log, never a union).
- `GET /operations/map/parcels/{id}`: the parcel's last confirmed spray, sprays this season and
  copper this year (`parcel_facts`); products and dose only from a log linking this parcel
  alone (F1). A parcel outside the request's farm: 404.

Plain HTML fragments (a `<section>`, no page chrome). Every read goes through the request's
`Repo` (`?farm=` before the cookie) and is bounded by `clock.now()`; links keep the farm query.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.operations import checks, register
from farmcore.apps.operations.parcel_facts import (
    COPPER,
    NOT_COPPER,
    UNIDENTIFIED,
    ProductLine,
    SprayLog,
    parcel_facts,
)
from farmcore.apps.operations.routes import PARCEL_SCOPE, _by_spray, _now
from farmcore.core.access import Repo, ScopedRepository
from farmcore.core.models.canonical import Log, LogParcel, Parcel, Product, Quantity
from farmcore.core.navigation import map_read_url, resolved_url, workspace_parent
from farmcore.web import render

router = APIRouter()

SCOPE = register.SCOPE  # "register:read", as the register
DAY_FORMAT = "%a %-d %b %Y"  # Wed 10 Apr 2024
# A parcel id the database can hold (bigint); anything else is a 422, never a 500.
ParcelId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _day(value: datetime, tz: str) -> str:
    return value.astimezone(ZoneInfo(tz)).strftime(DAY_FORMAT)


@router.get("/map/farm", response_class=HTMLResponse)
def farm_section(request: Request, repo: Repo) -> HTMLResponse:
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    sprays = _by_spray(register.register_rows(repo, now, tz=tz))
    latest: dict[str, Any] | None = None
    if sprays:
        rows = sprays[0]
        aliases = list(dict.fromkeys(r["parcel_alias"] for r in rows))
        first = [r for r in rows if r["parcel_alias"] == aliases[0]]
        same_day = sum(1 for s in sprays[1:] if s[0]["date"] == rows[0]["date"])
        latest = {
            "day": datetime.strptime(rows[0]["date"], "%Y-%m-%d").strftime(DAY_FORMAT),
            "parcels": aliases,
            "products": [
                ProductLine(r["product_name"], r["dose"], r["dose_unit"]).text for r in first
            ],
            "others": same_day,
        }
    return render(request, "operations/_map_farm.html", latest=latest)


def _product_line(
    q: Quantity | None, product: Product | None, entry: dict[str, Any] | None
) -> ProductLine:
    """One register product cell with its copper identity (`checks._known_copper`)."""
    cells = register._product_cells(q, product, entry)
    if product is not None and q is not None:  # a catalogue product always comes with its rate
        label = checks.Label.from_props(product.props)
        dose = checks.dose_kg_ha(q.value, q.unit)
        app = checks.Application(
            log_id=0,
            ts=datetime.min,
            day=datetime.min.date(),
            parcel="",
            product_id=product.id,
            product_name=product.name,
            substance=product.active_substance,
            dose_kg_ha=dose,
            label=label,
        )
        copper = checks._known_copper(app)
        known = dose is not None and label.cu_fraction is not None
        cu = dose * label.cu_fraction if known else None
        identity = COPPER if copper else NOT_COPPER
    else:
        cu = None
        substance = str(cells["active_substance"] or "").strip().casefold()
        identity = COPPER if substance == "copper" else UNIDENTIFIED
    return ProductLine(
        name=cells["product_name"],
        dose=cells["dose"],
        unit=cells["dose_unit"],
        identity=identity,
        cu_kg_ha=cu if identity == COPPER else None,
    )


def _spray_logs(repo: ScopedRepository, parcel_id: int, now: datetime, tz: str) -> list[SprayLog]:
    """The confirmed spray logs linked to the parcel up to `now`, each with every linked alias
    (the F1 test) and, only when it links this parcel alone, its register products."""
    ids = {lp.log_id for lp in repo.list(LogParcel, LogParcel.parcel_id == parcel_id, scope=SCOPE)}
    if not ids:
        return []
    spray_logs = repo.list(
        Log,
        Log.id.in_(ids),
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        scope=SCOPE,
        order_by=(Log.ts_start, Log.id),
    )
    if not spray_logs:
        return []
    log_ids = [lg.id for lg in spray_logs]
    links = repo.list(LogParcel, LogParcel.log_id.in_(log_ids), scope=SCOPE)
    parcels = {
        p.id: p
        for p in repo.list(Parcel, Parcel.id.in_({lp.parcel_id for lp in links}), scope=SCOPE)
    }
    linked: dict[int, list[Parcel | int]] = defaultdict(list)
    for lp in links:
        linked[lp.log_id].append(parcels.get(lp.parcel_id, lp.parcel_id))
    # F1 before any product: only a log linking this parcel alone has its quantities read.
    own_ids = [lg.id for lg in spray_logs if len(linked[lg.id]) == 1]
    quantities = (
        repo.list(Quantity, Quantity.log_id.in_(own_ids), scope=SCOPE, order_by=Quantity.id)
        if own_ids
        else []
    )
    quantities_of: dict[int, list[Quantity]] = defaultdict(list)
    for q in quantities:
        quantities_of[q.log_id].append(q)
    product_ids = {q.product_id for q in quantities if q.product_id is not None}
    products = (
        {p.id: p for p in repo.list(Product, Product.id.in_(product_ids), scope=SCOPE)}
        if product_ids
        else {}
    )
    zone = ZoneInfo(tz)
    out = []
    for lg in spray_logs:
        # A linked parcel this farm cannot see still makes the log shared, never this parcel's.
        seen = [p for p in linked[lg.id] if isinstance(p, Parcel)]
        visible = sorted(seen, key=register.parcel_key)
        hidden = ["another parcel" for p in linked[lg.id] if not isinstance(p, Parcel)]
        items = (
            [
                _product_line(q, products.get(q.product_id) if q and q.product_id else None, e)
                for q, e in register._product_items(lg, quantities_of[lg.id])
            ]
            if lg.id in own_ids
            else []  # shared: no product, dose or copper is read for this parcel
        )
        out.append(
            SprayLog(
                log_id=lg.id,
                ts=lg.ts_start,
                day=lg.ts_start.astimezone(zone).date(),
                parcels=(*(p.alias for p in visible), *hidden),
                products=tuple(items),
            )
        )
    return out


@router.get("/map/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_section(parcel_id: ParcelId, request: Request, repo: Repo) -> HTMLResponse:
    parcel = repo.get(Parcel, parcel_id, scope=PARCEL_SCOPE)
    if parcel is None:
        raise HTTPException(status_code=404, detail=f"parcel {parcel_id} not found")
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    today = now.astimezone(ZoneInfo(tz)).date()
    facts = parcel_facts(parcel.alias, _spray_logs(repo, parcel.id, now, tz), now, today)
    parent = workspace_parent(request, repo, parcel.id, "overview")
    detail_url = map_read_url(request, f"/operations/parcels/{parcel.id}", parcel.id)
    poll_url = resolved_url(request, f"/operations/map/parcels/{parcel.id}")
    if parent:
        parts = urlsplit(parent)
        plain = parts._replace(
            query=urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "return_to"])
        ).geturl()
        detail_url = (
            resolved_url(request, f"/operations/parcels/{parcel.id}")
            + "&"
            + urlencode({"return_to": plain})
        )
        poll_url += "&" + urlencode({"parent": parent})
    return render(
        request,
        "operations/_map_parcel.html",
        parcel_id=parcel.id,
        detail_url=detail_url,
        poll_url=poll_url,
        facts=facts,
        last_day=_day(facts.last.ts, tz) if facts.last else "",
        year=today.year,
    )
