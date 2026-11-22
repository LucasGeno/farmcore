"""Global FieldID (Varda): an owner looks up a parcel's id and keeps it as a portable alias.

Sources (Varda's developer docs, read 2026-11-22):

- Token: `POST https://auth.varda.ag/oauth/token`, a form with `grant_type=client_credentials`,
  `client_id`, `client_secret` and `audience=https://api.varda.ag/fid/`; JSON `access_token`,
  `token_type` and `expires_in` (seconds). https://developer.varda.ag/reference/post_oauth-token.md
- Lookup: `POST https://api.varda.ag/fid/v1/field-searches`, body `application/geo+json` (a Point,
  Polygon, MultiPolygon or Feature); 200 `{"entries": [{field_id, boundary_id, iou_score,
  input_based_intersection, output_based_intersection}]}`, by relevance, the three figures only
  for a polygon. https://developer.varda.ag/reference/field_searches.md
- Same field: "An 80% agreement (IoU 0.8) indicates that it's reasonable to say that the input
  polygon represents the same field." https://developer.varda.ag/docs/field-id-lookups.md

Off by default: without `VARDA_CLIENT_ID` and `VARDA_CLIENT_SECRET` no action shows and nothing
calls out, and a replay clock never calls out. The parcel's own shape is sent; a match with
IoU >= 0.8 is kept, anything less is shown with its overlap figures for the owner to choose.
Only ids are kept, as `ext_id` rows: `varda-gfid` (the field) and `varda-gfid-boundary` (its
active BoundaryID, to notice a changed boundary later). Varda geometries are never asked for or
stored: their licence is unverified. `ext_id` is unique on `(source, key)` across the
installation, so a field id another parcel holds is refused ("already claimed in this
installation"), never moved. The credentials go only to the token URL; no error this module
raises carries them or the token.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from farmcore.core import ext_ids
from farmcore.core.access import Repo, ScopedRepository, require_farm_owner
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.models.canonical import ExtId, Parcel
from farmcore.core.navigation import resolved_url
from farmcore.settings import Settings
from farmcore.web import render

GFID_SOURCE = "varda-gfid"
BOUNDARY_SOURCE = "varda-gfid-boundary"
SOURCES = (GFID_SOURCE, BOUNDARY_SOURCE)
LICENCE = "ids only; licence of Varda data unverified (no geometry stored)"
AUDIENCE = "https://api.varda.ag/fid/"
# Varda's own threshold for "the same field" (field-id-lookups, above).
IOU_SAME_FIELD = 0.8
TOKEN_MARGIN_S = 30.0  # renew a little before `expires_in` runs out
TIMEOUT_S = 10.0
RESOLVER = "https://fieldid.varda.ag/fields/{}"
SCOPE = "fields:read"

router = APIRouter()
ParcelId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class VardaError(RuntimeError):
    """Varda refused or failed a call. The message names the step and the HTTP status only."""


class AlreadyClaimed(ValueError):
    """Another parcel in this installation already holds the field id."""


@dataclass(frozen=True)
class Candidate:
    field_id: str
    boundary_id: str
    iou: float | None
    input_intersection: float | None
    output_intersection: float | None


@dataclass(frozen=True)
class Lookup:
    kept: Candidate | None
    candidates: list[Candidate]


class VardaClient:
    """Read-only Global FieldID calls with one cached client-credentials token, renewed when its
    `expires_in` runs out or once when the API refuses it."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        api_url: str,
        token_url: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "audience": AUDIENCE,
        }
        self.api_url, self.token_url = api_url.rstrip("/"), token_url
        self._transport, self._clock, self._timeout = transport, clock, timeout
        self._token: str | None = None
        self._expires_at = 0.0

    def _send(self, step: str, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                return client.request(method, url, **kw)
        except httpx.HTTPError as exc:  # its text may hold the URL; the name is enough
            raise VardaError(f"{step}: {type(exc).__name__}") from None

    def _bearer(self) -> str:
        if self._token is None or self._clock() >= self._expires_at:
            response = self._send("token", "POST", self.token_url, data=self._form)
            if response.status_code != 200:
                raise VardaError(f"token: HTTP {response.status_code}")
            body = response.json()
            token, lifetime = body.get("access_token"), body.get("expires_in")
            if not isinstance(token, str) or not token:
                raise VardaError("token: no access token in the response")
            self._token = token
            # No lifetime: keep it until the API refuses it.
            ttl = float(lifetime) if isinstance(lifetime, int | float) else float("inf")
            self._expires_at = self._clock() + ttl - TOKEN_MARGIN_S
        return self._token

    def search(self, geometry: dict) -> list[Candidate]:
        """`POST /field-searches` with a GeoJSON geometry: the matching fields, by relevance."""
        for renew in (False, True):
            if renew:
                self._token = None
            response = self._send(
                "field-searches",
                "POST",
                f"{self.api_url}/field-searches",
                content=json.dumps(geometry),
                headers={
                    "Authorization": f"Bearer {self._bearer()}",
                    "Content-Type": "application/geo+json",
                },
            )
            if response.status_code != 401:
                break
        if response.status_code != 200:
            raise VardaError(f"field-searches: HTTP {response.status_code}")
        return [_candidate(entry) for entry in response.json().get("entries") or []]


def _figure(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _candidate(entry: dict) -> Candidate:
    field, boundary = entry.get("field_id"), entry.get("boundary_id")
    if not isinstance(field, str) or not isinstance(boundary, str):
        raise VardaError("field-searches: an entry without field_id or boundary_id")
    return Candidate(
        field,
        boundary,
        _figure(entry.get("iou_score")),
        _figure(entry.get("input_based_intersection")),
        _figure(entry.get("output_based_intersection")),
    )


def client_from(settings: Settings) -> VardaClient | None:
    """The installation's client, or None without both credentials."""
    client_id, secret = settings.varda_client_id, settings.varda_client_secret
    if not (client_id and client_id.get_secret_value() and secret and secret.get_secret_value()):
        return None
    return VardaClient(
        client_id.get_secret_value(),
        secret.get_secret_value(),
        api_url=settings.varda_api_base_url,
        token_url=settings.varda_token_url,
    )


def get_client(request: Request) -> VardaClient | None:
    """FastAPI dependency: one client per app, so its token is reused; None when not configured."""
    state = request.app.state
    if not hasattr(state, "varda_client"):
        state.varda_client = client_from(state.settings)
    return state.varda_client


def can_call(request: Request, repo: ScopedRepository, client: VardaClient | None) -> bool:
    """Credentials and a live clock: a replay never calls out."""
    settings: Settings = request.app.state.settings
    return client is not None and isinstance(get_clock(repo.session, settings), LiveClock)


def _geometry(repo: ScopedRepository, parcel: Parcel) -> dict:
    statement = select(func.ST_AsGeoJSON(Parcel.geom)).where(
        Parcel.id == parcel.id, Parcel.farm_id == repo.farm_id
    )
    return json.loads(repo.execute(statement, scope=SCOPE, resource="parcel").scalar_one())


def look_up(repo: ScopedRepository, parcel: Parcel, client: VardaClient) -> Lookup:
    """Search Varda with the parcel's shape; keep the best match when it is the same field."""
    candidates = client.search(_geometry(repo, parcel))
    scored = [c for c in candidates if c.iou is not None]
    best = max(scored, key=lambda c: c.iou or 0.0, default=None)
    if best is not None and (best.iou or 0.0) >= IOU_SAME_FIELD:
        keep(repo, parcel, best)
        return Lookup(best, candidates)
    return Lookup(None, candidates)


def keep(repo: ScopedRepository, parcel: Parcel, candidate: Candidate) -> None:
    """Record `candidate` as the parcel's Global FieldID, replacing an earlier one. A field or
    boundary id another parcel holds is refused, whichever farm it is on."""
    session = repo.session
    ids = ((GFID_SOURCE, candidate.field_id), (BOUNDARY_SOURCE, candidate.boundary_id))
    claimed = AlreadyClaimed(
        f"Global FieldID {candidate.field_id} is already claimed in this installation."
    )
    for source, key in ids:
        held = session.scalar(select(ExtId).where(ExtId.source == source, ExtId.key == key))
        if held is not None and (held.model, held.res_id) != (Parcel.__tablename__, parcel.id):
            raise claimed
    ext_ids.ensure_source(session, GFID_SOURCE, "Varda Global FieldID", "registry", LICENCE)
    try:
        with session.begin_nested():
            session.execute(
                delete(ExtId).where(
                    ExtId.model == Parcel.__tablename__,
                    ExtId.res_id == parcel.id,
                    ExtId.source.in_(SOURCES),
                )
            )
            session.add_all(
                ExtId(source=source, key=key, model=Parcel.__tablename__, res_id=parcel.id)
                for source, key in ids
            )
            session.flush()
    except IntegrityError:  # claimed by a concurrent lookup
        raise claimed from None


def alias_of(repo: ScopedRepository, parcel: Parcel) -> str | None:
    """The parcel's kept Global FieldID, or None."""
    statement = select(ExtId.key).where(
        ExtId.source == GFID_SOURCE,
        ExtId.model == Parcel.__tablename__,
        ExtId.res_id == parcel.id,
    )
    return repo.execute(statement, scope=SCOPE, resource="ext_id").scalar_one_or_none()


Client = Annotated[VardaClient | None, Depends(get_client)]


def _ready(request: Request, repo: Repo, client: VardaClient | None, parcel_id: int) -> Parcel:
    require_farm_owner(request, repo)
    if client is None:
        raise HTTPException(404, "Global FieldID lookups are not configured.")
    parcel = repo.get(Parcel, parcel_id, scope=SCOPE)
    if parcel is None:
        raise HTTPException(404, "No such parcel on this farm.")
    if not can_call(request, repo, client):
        raise HTTPException(409, "The clock is a replay: Global FieldID lookups need live mode.")
    return parcel


def _guarded(run: Callable[[], Any]) -> Any:
    """Run a Varda step, turning its refusals into HTTP answers."""
    try:
        return run()
    except VardaError as exc:
        raise HTTPException(502, f"Global FieldID: {exc}") from None
    except AlreadyClaimed as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/parcels/{parcel_id}/field-id/lookup", response_class=HTMLResponse)
def lookup_page(request: Request, repo: Repo, client: Client, parcel_id: ParcelId) -> HTMLResponse:
    """Look the parcel up: a same-field match is kept, otherwise the candidates are listed."""
    parcel = _ready(request, repo, client, parcel_id)
    result = _guarded(lambda: look_up(repo, parcel, client))
    repo.session.commit()
    return render(
        request,
        "fields/field_id.html",
        parcel=parcel,
        result=result,
        threshold=IOU_SAME_FIELD,
        parcel_url=resolved_url(request, f"/fields/parcels/{parcel.id}"),
        keep_url=resolved_url(request, f"/fields/parcels/{parcel.id}/field-id"),
        resolver=RESOLVER,
    )


@router.post("/parcels/{parcel_id}/field-id")
def keep_choice(
    request: Request,
    repo: Repo,
    client: Client,
    parcel_id: ParcelId,
    field_id: Annotated[str, Form(max_length=9)],
) -> RedirectResponse:
    """Keep the candidate the owner chose; it must still be one Varda returns for this parcel."""
    parcel = _ready(request, repo, client, parcel_id)
    candidates = _guarded(lambda: client.search(_geometry(repo, parcel)))
    chosen = next((c for c in candidates if c.field_id == field_id), None)
    if chosen is None:
        raise HTTPException(422, "That Global FieldID is not a candidate for this parcel.")
    _guarded(lambda: keep(repo, parcel, chosen))
    repo.session.commit()
    return RedirectResponse(resolved_url(request, f"/fields/parcels/{parcel.id}"), 303)
