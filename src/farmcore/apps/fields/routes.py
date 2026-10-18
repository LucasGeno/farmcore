"""Fields routes (mounted at `/fields`): the parcel GeoJSON, the map page and its panel.

The panel is a shell: Farm today (`GET /fields/panel`) or one parcel's facts
(`GET /fields/panel/parcels/{id}`, 404 outside the selected farm), then one shared fragment slot
(`core/_fragment.html`) per section URL a visible app declares (`farm_sections`,
`parcel_sections`), in manifest load order. Fields never imports app code; each app's endpoint
owns its facts and its own farm scope."""

from __future__ import annotations

import re
import secrets
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from farmcore.apps.fields.service import (
    NOT_ADVISED_LABEL,
    parcels_feature_collection,
    styles_for,
)
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.manifest import collect_parcel_views
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.navigation import (
    bounded_query,
    reading_destination,
    resolved_url,
    return_destination,
)
from farmcore.core.registry import visible_manifests
from farmcore.settings import Settings
from farmcore.web import context_url, render

router = APIRouter()

ADVICE_LAYER = "advice"  # Crop protection's layer: the decision legend and the card links

SYNTHETIC_LAYOUT = "synthetic"
# Shown on the map when the farm's props say `layout: "synthetic"` (no real geometry).
LAYOUT_BANNER = (
    "synthetic layout: imported parcel shapes in local metres, placed on the {region} centroid"
)


def layout_banner(repo: Repo) -> str | None:
    farm = repo.get(Farm, repo.farm_id, scope="fields:read")
    props = (farm.props or {}) if farm else {}
    if props.get("map_notice"):
        return str(props["map_notice"])
    if props.get("layout") != SYNTHETIC_LAYOUT:
        return None
    return LAYOUT_BANNER.format(region=props.get("nuts3", "regional"))


def map_layers(request: Request, *, freeze_farm: bool = False) -> list[dict]:
    """The visible apps' map layers, in `order` (ties keep load order), as the JSON the map
    script reads. An app switched off on /apps drops out at once."""
    layers = sorted(
        (layer for m in visible_manifests(request) for layer in m.map_layers),
        key=lambda layer: layer.order,
    )
    return [
        {
            "key": layer.key,
            "label": layer.label,
            "url": resolved_url(request, layer.url)
            if freeze_farm
            else context_url(request, layer.url),
            "kind": layer.kind,
            "default_on": layer.default_on,
            "icon": layer.icon,
        }
        for layer in layers
    ]


def _map_context(request: Request, *, freeze_farm: bool = False) -> dict:
    """Layers plus the colouring the page opens with: the `default_on` parcels layer (else the
    first one); None leaves the parcels neutral."""
    layers = map_layers(request, freeze_farm=freeze_farm)
    parcels = [layer for layer in layers if layer["kind"] == "parcels"]
    active = next((layer for layer in parcels if layer["default_on"]), None) or next(
        iter(parcels), None
    )
    return {
        "layers": layers,
        "colouring": active["key"] if active else None,
        "advice_on": any(layer["key"] == ADVICE_LAYER for layer in layers),
    }


def _collection(request: Request, repo: Repo) -> dict:
    settings: Settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    return with_card_urls(request, parcels_feature_collection(repo, now, tz=settings.display_tz))


def with_card_urls(request: Request, collection: dict) -> dict:
    """Each feature's `card_url` keeps an explicit `?farm=` (`context_url`)."""
    for feature in collection["features"]:
        props = feature["properties"]
        if props.get("card_url"):
            props["card_url"] = context_url(request, props["card_url"])
    return collection


PanelKind = Literal["farm", "parcel"]
PANEL_FARM_URL = "/fields/panel"
PANEL_PARCEL_URL = "/fields/panel/parcels/{id}"
_MAX_ID = 2**31 - 1  # Postgres `integer` primary keys
_PARCEL_ID = re.compile(r"[0-9]{1,9}")  # ASCII only: `str.isdigit` accepts "²", which int() rejects
ALIEN_PARCEL_NOTE = "That parcel is not on this farm."


def _token(name: str) -> str:
    """An app name as a CSS-safe id part."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def panel_sections(request: Request, kind: PanelKind, parcel_id: int | None = None) -> list[dict]:
    """One slot per section URL of the visible apps, in load order: `{id}` filled with the parcel
    id, then the URL farm-qualified (`context_url`) before it reaches the slot macro. Parcel
    advice leads and core provenance follows the app facts. Slot ids
    are `sec-<app>-<kind>-<n>-<nonce>`: the per-mount nonce keeps a new mount's targets apart
    from an old mount's in-flight requests. A hidden app contributes nothing."""
    nonce = secrets.token_hex(4)
    sections = []
    manifests = visible_manifests(request)
    if kind == "parcel":
        # Put the decision first and the core provenance summary last; other apps stay stable.
        manifests = sorted(manifests, key=lambda m: -1 if m.name == "advisor" else m.name == "core")
    for manifest in manifests:
        urls = manifest.parcel_sections if kind == "parcel" else manifest.farm_sections
        for n, url in enumerate(urls, start=1):
            label = dict(manifest.section_labels).get(url, manifest.display_name)
            if kind == "parcel":
                url = url.replace("{id}", str(int(parcel_id or 0)))
            elif "{id}" in url:
                continue
            sections.append(
                {
                    "slot_id": f"sec-{_token(manifest.name)}-{kind}-{n}-{nonce}",
                    "label": label,
                    "app": manifest.name,
                    "url": resolved_url(request, url),
                }
            )
    return sections


def _panel(request: Request, repo: Repo, parcel: Parcel | None, note: str | None = None) -> dict:
    if parcel is None:
        farm = repo.get(Farm, repo.farm_id, scope="fields:read")
        return {
            "kind": "farm",
            "parcel": None,
            "farm_name": farm.name if farm else "",
            "sections": panel_sections(request, "farm"),
            "note": note,
        }
    return {
        "kind": "parcel",
        "parcel": {
            "id": parcel.id,
            "alias": parcel.alias,
            "name": parcel.name,
            "area_ha": float(parcel.area_ha),
            "variety": (parcel.props or {}).get("variety"),
        },
        "sections": panel_sections(request, "parcel", parcel.id),
        "close_url": resolved_url(request, "/fields/"),
        "note": note,
    }


def _scoped_parcel(repo: Repo, parcel_id: int) -> Parcel | None:
    """The parcel when it belongs to the selected farm, else None (no other farm's facts). An id
    outside the integer key range is no parcel (never a database error)."""
    if not 1 <= parcel_id <= _MAX_ID:
        return None
    return repo.get(Parcel, parcel_id, scope="fields:read")


@router.get("/panel", response_class=HTMLResponse)
def farm_panel(request: Request, repo: Repo) -> HTMLResponse:
    return render(request, "fields/_panel.html", panel=_panel(request, repo, None))


@router.get("/panel/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_panel(request: Request, repo: Repo, parcel_id: int) -> HTMLResponse:
    parcel = _scoped_parcel(repo, parcel_id)
    if parcel is None:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    return render(request, "fields/_panel.html", panel=_panel(request, repo, parcel))


@router.get("/parcels.geojson")
def parcels_geojson(request: Request, repo: Repo) -> JSONResponse:
    return JSONResponse(_collection(request, repo), media_type="application/geo+json")


@router.get("/today", response_class=HTMLResponse)
def today_map(
    request: Request, repo: Repo, desktop: bool = False, compact: bool = False
) -> HTMLResponse:
    """The Today map fragment: legend, a shorter map and the basemap toggle, with its own assets
    (no page wrapper), so the Today home can embed it. A parcel tap dispatches
    `farmcore:parcel-tap`. The parcels arrive by the fragment's own `parcels.geojson` fetch, so
    the HTML needs only one cheap scoped read for the access log."""
    repo.get(Farm, repo.farm_id, scope="fields:read")
    if desktop:  # backwards-compatible Home preview shell at every width
        return render(request, "fields/_today_map_desktop.html")
    return render(
        request,
        "fields/_today_map.html",
        map_class="h-[260px]" if compact else "fc-map-today",
        styles=styles_for(request),
        not_advised_label=NOT_ADVISED_LABEL,
        **_map_context(request, freeze_farm=True),
    )


@router.get("/list", response_class=HTMLResponse)
def parcel_list(request: Request, repo: Repo) -> HTMLResponse:
    """The map's parcel cards, refreshed independently of its camera and selected panel."""
    return render(
        request,
        "fields/_parcel_list.html",
        parcels=[f["properties"] for f in _collection(request, repo)["features"]],
        styles=styles_for(request),
        not_advised_label=NOT_ADVISED_LABEL,
        **_map_context(request),
    )


def add_query(url: str, values: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    return parts._replace(query=urlencode({**query, **values})).geturl()


def provider_query(request: Request, keys: tuple[str, ...]) -> dict[str, str]:
    """Only bounded, unambiguous provider state. Semantic interpretation belongs to apps."""
    return {
        key: values[0]
        for key in keys
        if len(values := request.query_params.getlist(key)) == 1 and bounded_query(values[0])
    }


@router.get("/parcels/{parcel_id}", response_class=HTMLResponse)
def workspace(request: Request, repo: Repo, parcel_id: int) -> HTMLResponse:
    parcel = _scoped_parcel(repo, parcel_id)
    if parcel is None:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    destination = return_destination(
        request, repo, f"/fields/?parcel={parcel.id}#map-panel-heading", "Back to Map"
    )
    views = collect_parcel_views(visible_manifests(request))
    selected = request.query_params.get("view", "overview")
    descriptor = next((v for _, v in views if v.key == selected), None)
    notice = None
    if selected != "overview" and descriptor is None:
        notice = "No longer available: that parcel view. Showing Overview."
        selected = "overview"
    query = provider_query(request, descriptor.query_keys) if descriptor else {}
    base = f"/fields/parcels/{parcel.id}"
    current = resolved_url(request, base + "?" + urlencode({"view": selected, **query}))
    origins = request.query_params.getlist("return_to")
    validated = reading_destination(request, repo, origins[0]) if len(origins) == 1 else None
    origin = validated.url if validated else None
    parent = add_query(current, {"return_to": origin}) if origin else current
    tabs = [{"key": "overview", "label": "Overview"}] + [
        {"key": v.key, "label": v.label} for _, v in views
    ]
    for tab in tabs:
        state = query if tab["key"] == selected else {}
        tab["url"] = resolved_url(
            request,
            base
            + "?"
            + urlencode({"view": tab["key"], **state, **({"return_to": origin} if origin else {})}),
        )
    section = None
    if descriptor:
        section = {
            "slot_id": f"workspace-{selected}-{secrets.token_hex(4)}",
            "label": descriptor.label,
            "url": add_query(
                resolved_url(request, descriptor.url.replace("{id}", str(parcel.id))),
                {**query, "parent": parent},
            ),
            "fallback_url": add_query(
                resolved_url(request, descriptor.fallback_url.replace("{id}", str(parcel.id))),
                {**query, "return_to": current},
            ),
        }
    sections = panel_sections(request, "parcel", parcel.id) if not descriptor else []
    owners = {m.name: m for m in visible_manifests(request)}
    for item in sections:
        item["url"] = add_query(item["url"], {"parent": parent})
        owner = owners[item["app"]]
        item["fallback_url"] = resolved_url(request, owner.nav[1]) if owner.nav else None
    return render(
        request,
        "fields/workspace.html",
        parcel=parcel,
        destination=destination,
        views=tabs,
        selected_view=selected,
        notice=notice,
        section=section,
        sections=sections,
    )


@router.get("/", response_class=HTMLResponse)
def map_page(
    request: Request, repo: Repo, parcel: str | None = None, view: str = "map", q: str = ""
) -> HTMLResponse:
    """The map with its panel rendered open: `?parcel=<id>` (a list row's "Show on map", a
    shared link, Back) opens that parcel with no script; otherwise Farm today. A parcel outside
    the selected farm, or a malformed id, falls back to Farm today with a note and nothing of
    that parcel; an empty `?parcel=` is no selection."""
    if view == "list":
        rows = (
            repo.execute(
                select(Parcel).where(Parcel.farm_id == repo.farm_id),
                scope="fields:read",
                resource="parcel",
            )
            .scalars()
            .all()
        )
        matches = [
            p
            for p in rows
            if not q
            or any(
                q.casefold() in str(value).casefold()
                for value in (
                    p.alias,
                    p.name,
                    p.iacs_id,
                    (p.props or {}).get("variety"),
                    (p.props or {}).get("crop_label"),
                )
                if value is not None
            )
        ]
        origin = resolved_url(request, "/fields/?" + urlencode({"view": "list", "q": q}))
        return render(
            request,
            "fields/list.html",
            parcels=matches if len(q) <= 80 else [],
            q=q[:80],
            total=len(rows),
            search_error=len(q) > 80,
            parcel_urls={
                p.id: resolved_url(
                    request,
                    f"/fields/parcels/{p.id}?"
                    + urlencode({"return_to": origin + f"#parcel-{p.id}"}),
                )
                for p in matches
            },
        )
    parcels = [f["properties"] for f in _collection(request, repo)["features"]]
    asked = parcel.strip() if parcel else ""
    selected = _scoped_parcel(repo, int(asked)) if _PARCEL_ID.fullmatch(asked) else None
    note = ALIEN_PARCEL_NOTE if asked and selected is None else None
    return render(
        request,
        "fields/map.html",
        parcels=parcels,
        panel=_panel(request, repo, selected, note),
        panel_farm_url=resolved_url(request, PANEL_FARM_URL),
        panel_parcel_url=resolved_url(request, PANEL_PARCEL_URL),
        styles=styles_for(request),
        not_advised_label=NOT_ADVISED_LABEL,
        layout_banner=layout_banner(repo),
        mode_notice="Unknown presentation. Showing Map." if view != "map" else None,
        **_map_context(request, freeze_farm=True),
    )
