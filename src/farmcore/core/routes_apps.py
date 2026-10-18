"""`/apps`: the app grid and switching apps on and off.

- `GET /apps`: tiles grouped by category, in farmer words; switches for the admin only;
  version, maturity and load order only in Expert view.
- `GET /apps.json`: every app in load order (name, title, category, version, summary, depends,
  state, maturity, loaded, required, removable).
- `POST /apps/{name}/uninstall|install` (admin): only change `app_module.state`. The UI follows
  at once for apps loaded at start (`registry.visible_manifests`); routes load or unload at the
  next start. Required apps (and core) cannot be switched off. Refusals (409) name apps by
  their title: "Crop protection needs Weather. Switch off Crop protection first."

JSON by default; a browser form post (`Accept: text/html`) gets a redirect back to `/apps`,
or the page with the refusal and status 409.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from farmcore.core.manifest import Manifest
from farmcore.core.models.kernel import AppModule
from farmcore.core.registry import CORE, INSTALLED, UNINSTALLED, active_manifests, read_states
from farmcore.core.routes_clock import require_admin
from farmcore.db import get_session
from farmcore.web import context_url, render

router = APIRouter()


def _ordered(request: Request) -> list[Manifest]:
    return list(getattr(request.app.state, "all_manifests", request.app.state.manifests))


def _states(request: Request, session: Session) -> dict[str, str]:
    return read_states(session, _ordered(request))


def _required(m: Manifest) -> bool:
    return m.required or m.name == CORE.name


def _listing(request: Request, session: Session) -> list[dict[str, Any]]:
    states = _states(request, session)
    loaded = {m.name for m in request.app.state.manifests}
    return [
        {
            "name": m.name,
            "title": m.display_name,
            "category": m.category,
            "version": m.version,
            "summary": m.summary,
            "depends": list(m.depends),
            "state": states[m.name],
            "maturity": m.maturity,
            "loaded": m.name in loaded,
            "required": _required(m),
            "removable": not _required(m),
        }
        for m in _ordered(request)
    ]


def _join(names: list[str]) -> str:
    """`A`, `A and B`, `A, B and C`."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _status(app: dict[str, Any]) -> str:
    """`always` (required), `off`, `on`, or `restart` (installed, loads at the next start)."""
    if app["required"]:
        return "always"
    if app["state"] == UNINSTALLED:
        return "off"
    return "on" if app["loaded"] else "restart"


ESSENTIALS = "Essentials"
OTHER = "Other"


def _groups(request: Request, listing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tiles grouped by category: Essentials first, then alphabetical, uncategorised last as
    Other; inside a group alphabetical by title. Core has no tile. A dependency is named only
    when the farm can switch it off (required apps are always on)."""
    by_name = {m.name: m for m in _ordered(request)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for app in listing:
        if app["name"] == CORE.name:
            continue
        m = by_name[app["name"]]
        needs = [
            by_name[d].display_name for d in m.depends if d in by_name and not _required(by_name[d])
        ]
        status = _status(app)
        groups.setdefault(m.category or OTHER, []).append(
            {
                **app,
                "icon": m.nav_icon,
                "needs": _join(needs) if needs else "",
                "status": status,
                "href": m.nav[1] if m.nav and status in ("on", "always") else None,
                "links": [item for item in m.extra_nav if item.group != "admin"]
                if status in ("on", "always")
                else [],
                "switch": None
                if status == "always"
                else ("install" if status == "off" else "uninstall"),
            }
        )

    def key(category: str) -> tuple[int, str]:
        return (0 if category == ESSENTIALS else 2 if category == OTHER else 1, category.casefold())

    return [
        {"category": c, "apps": sorted(groups[c], key=lambda a: a["title"].casefold())}
        for c in sorted(groups, key=key)
    ]


def _page(request: Request, session: Session, error: str | None = None) -> Response:
    listing = _listing(request, session)
    return render(
        request, "core/apps.html", apps=listing, groups=_groups(request, listing), error=error
    )


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _manifest(request: Request, name: str) -> Manifest:
    for m in _ordered(request):
        if m.name == name:
            return m
    raise HTTPException(status_code=404, detail=f"no app named {name!r}")


def _refuse(request: Request, session: Session, detail: str, **extra: Any) -> Response:
    if _wants_html(request):
        resp = _page(request, session, error=detail)
        resp.status_code = 409
        return resp
    return JSONResponse({"detail": detail, **extra}, status_code=409)


def _set_state(request: Request, session: Session, manifest: Manifest, state: str) -> Response:
    row = session.get(AppModule, manifest.name)
    if row is None:
        row = AppModule(name=manifest.name, version=manifest.version)
        session.add(row)
    if row.state != state:
        row.state = state
    session.commit()
    if _wants_html(request):
        return RedirectResponse(context_url(request, "/apps"), status_code=303)
    return JSONResponse(
        {"name": manifest.name, "state": state, "applies": "at next start", "ui": "now"}
    )


@router.get("/apps")
def apps_page(request: Request, session: Session = Depends(get_session)) -> Response:
    return _page(request, session)


@router.get("/apps.json")
def apps_json(request: Request, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return _listing(request, session)


@router.post("/apps/{name}/uninstall", dependencies=[Depends(require_admin)])
def uninstall(name: str, request: Request, session: Session = Depends(get_session)) -> Response:
    manifest = _manifest(request, name)
    if _required(manifest):
        detail = f"{manifest.display_name} is always on."
        return _refuse(request, session, detail, dependants=[])
    states = _states(request, session)
    dependants = [m for m in _ordered(request) if name in m.depends and states[m.name] == INSTALLED]
    if dependants:
        titles = [m.display_name for m in dependants]
        this = manifest.display_name
        if len(titles) == 1:
            detail = f"{titles[0]} needs {this}. Switch off {titles[0]} first."
        else:
            detail = f"{_join(titles)} need {this}. Switch them off first."
        return _refuse(request, session, detail, dependants=[m.name for m in dependants])
    return _set_state(request, session, manifest, UNINSTALLED)


@router.post("/apps/{name}/install", dependencies=[Depends(require_admin)])
def install(name: str, request: Request, session: Session = Depends(get_session)) -> Response:
    manifest = _manifest(request, name)
    states = _states(request, session)
    # A dependency that will not load at the next start (uninstalled, or installed but missing
    # its own dependency) would leave this app unloaded too.
    loading = {m.name for m in active_manifests(_ordered(request), states)} | {CORE.name}
    missing = [d for d in manifest.depends if d not in loading]
    if missing:
        by_name = {m.name: m for m in _ordered(request)}
        titles = [by_name[d].display_name if d in by_name else d for d in missing]
        this = manifest.display_name
        if len(titles) == 1:
            detail = f"{this} needs {titles[0]}. Switch on {titles[0]} first."
        else:
            detail = f"{this} needs {_join(titles)}. Switch them on first."
        return _refuse(request, session, detail, uninstalled_dependencies=missing)
    return _set_state(request, session, manifest, INSTALLED)
