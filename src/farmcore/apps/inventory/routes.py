"""`GET /inventory/` (the stock list of the selected farm) and `POST /inventory/{id}/low-stock`.

The page reads through the request's `Repo` (`service.stock_rows` at the clock's now). The only
write is the farmer's low-stock threshold (0 to `MAX_LOW_STOCK`, at most 3 decimals): admin only
(`require_admin`; tour mode is never admin, and the tour middleware blocks every non-GET), on a
row of the selected farm (else 404). An empty value clears it; anything else (negative, too
large, too precise, not a number) is a 422: for a browser, the page again with the typed value
and the message beside that row's field. No purchase form tonight: rows come from the seed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from farmcore.apps.inventory import service
from farmcore.apps.inventory.models import InventoryStock
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock
from farmcore.core.errors import wants_html
from farmcore.core.models.canonical import Farm
from farmcore.web import context_url, render

router = APIRouter()

MAX_LOW_STOCK = Decimal(1_000_000)
STEP = Decimal("0.001")
MAX_INPUT = 32  # characters; longer input is refused before parsing
INVALID_MESSAGE = f"Enter a number from 0 to {MAX_LOW_STOCK}, with at most 3 decimals."


def num(value: Decimal | None) -> str:
    """`16`, `2.5`, `0.125`: fixed notation, trailing zeros trimmed."""
    if value is None:
        return ""
    s = f"{value:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


@router.get("/", response_class=HTMLResponse)
def index(request: Request, repo: Repo) -> HTMLResponse:
    return _page(request, repo)


def _page(
    request: Request, repo: Repo, *, refused: tuple[int, str, str] | None = None
) -> HTMLResponse:
    """The stock list; `refused` (stock id, typed value, message) refills one row's form."""
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    farm = repo.get(Farm, repo.farm_id, scope=service.LOGS_SCOPE)
    catalogue = (farm.props or {}).get("product_ids") != [] if farm else False
    season, _ = service.season_start(now, settings.display_tz)
    return render(
        request,
        "inventory/index.html",
        rows=service.stock_rows(repo, now=now, tz=settings.display_tz),
        catalogue=catalogue,
        season=season,
        now=now,
        num=num,
        refused=refused,
    )


def _threshold(raw: str) -> Decimal | None:
    text = raw.strip()
    if text == "":
        return None
    invalid = HTTPException(
        status_code=422,
        detail=f"low stock must be a number from 0 to {MAX_LOW_STOCK}, at most 3 decimals",
    )
    if len(text) > MAX_INPUT:
        raise invalid
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation as exc:
        raise invalid from exc
    if not value.is_finite() or not 0 <= value <= MAX_LOW_STOCK:
        raise invalid
    rounded = value.quantize(STEP)
    if rounded != value:
        raise invalid
    return rounded


@router.post("/{stock_id}/low-stock", dependencies=[Depends(require_farm_owner)])
def set_low_stock(
    request: Request, stock_id: int, repo: Repo, low_stock: Annotated[str, Form()] = ""
) -> Response:
    row = repo.get(InventoryStock, stock_id, scope=service.LOGS_SCOPE)
    if row is None:
        raise HTTPException(status_code=404, detail="no such stock row on this farm")
    try:
        row.low_stock = _threshold(low_stock)
    except HTTPException:
        if not wants_html(request):
            raise
        typed = low_stock[:MAX_INPUT]
        page = _page(request, repo, refused=(stock_id, typed, INVALID_MESSAGE))
        page.status_code = 422
        page.headers["Cache-Control"] = "no-store"
        return page
    return RedirectResponse(context_url(request, "/inventory/"), status_code=303)
