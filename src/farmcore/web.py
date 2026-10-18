"""Shared Jinja templates: `core/templates` plus every `apps/*/templates` folder.

Template names are namespaced by folder: `core/base.html`, `fields/map.html`, ...
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, pass_context
from jinja2.runtime import Context
from starlette.responses import HTMLResponse

from farmcore.core.nav import active_path, build_nav
from farmcore.core.navigation import map_read_url as _map_read_url
from farmcore.core.navigation import resolved_url
from farmcore.core.registry import visible_manifests
from farmcore.core.tour import is_tour
from farmcore.core.urls import with_farm
from farmcore.settings import Settings

PACKAGE_DIR = Path(__file__).resolve().parent
LOCAL_FORMAT = "%d %b %Y %H:%M"


def template_dirs() -> list[Path]:
    dirs = [PACKAGE_DIR / "core" / "templates"]
    dirs += sorted(p for p in (PACKAGE_DIR / "apps").glob("*/templates") if p.is_dir())
    return dirs


def _display_tz(context: Context) -> str:
    request = context.get("request")
    settings = getattr(getattr(request, "app", None), "state", None)
    settings = getattr(settings, "settings", None)
    if isinstance(settings, Settings):
        return settings.display_tz
    return Settings.model_fields["display_tz"].default


@pass_context
def local(context: Context, value: datetime | None) -> str:
    """UTC datetime -> display timezone, `10 Apr 2024 06:00`. Naive values are taken as UTC."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(_display_tz(context))).strftime(LOCAL_FORMAT)


def explicit_farm(request: Request | None) -> int | None:
    """A resolved explicit selection, or an untrusted selector that endpoints must authorize."""
    if (
        request is None
        or not request.scope.get("query_string")
        or "farm" not in request.query_params
    ):
        return None
    selected = getattr(request.state, "selected_farm_id", request.query_params.get("farm"))
    try:
        farm_id = int(selected)
    except (TypeError, ValueError):
        return None
    return farm_id if farm_id > 0 else None


def context_url(request: Request | None, path: str) -> str:
    """Keep explicit farm context without changing global cookies or granting access."""
    farm_id = explicit_farm(request)
    return with_farm(path, farm_id) if farm_id is not None else path


@pass_context
def farm_url(context: Context, path: str) -> str:
    return context_url(context.get("request"), path)


@pass_context
def map_read_url(context: Context, path: str, parcel_id: int) -> str:
    return _map_read_url(context.get("request"), path, parcel_id)


@pass_context
def resolved_farm_url(context: Context, path: str) -> str:
    return resolved_url(context.get("request"), path)


templates = Jinja2Templates(directory=template_dirs()[0])
templates.env.loader = ChoiceLoader([FileSystemLoader(d) for d in template_dirs()])
templates.env.filters["local"] = local
templates.env.globals["farm_url"] = farm_url
templates.env.globals["map_read_url"] = map_read_url
templates.env.globals["resolved_farm_url"] = resolved_farm_url


def is_admin(request: Request) -> bool:
    """The admin check behind `require_admin`: with no `admin_token` configured everyone is admin;
    otherwise the token must come as `X-Admin-Token` header or `admin_token` cookie."""
    if is_tour(request):
        return False
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        return False
    token = settings.admin_token
    if token is None:
        return True
    given = request.headers.get("X-Admin-Token") or request.cookies.get("admin_token") or ""
    return secrets.compare_digest(given.encode(), token.get_secret_value().encode())


EXPERT_COOKIE = "expert"
SUNLIGHT_COOKIE = "sunlight"


def is_expert(request: Request) -> bool:
    """The viewer's "Expert view" preference (a plain cookie, not a permission)."""
    return request.cookies.get(EXPERT_COOKIE) == "1"


def is_sunlight(request: Request) -> bool:
    """The viewer's Sunlight (high-contrast) preference: a plain cookie, like Expert."""
    return request.cookies.get(SUNLIGHT_COOKIE) == "1"


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    """Render a template with the shell context: `manifests` (the visible apps: loaded and not
    switched off), `is_admin`, `expert`, and the grouped `nav` with the item matching the current
    path (`nav_current`)."""
    manifests = visible_manifests(request)
    admin = is_admin(request)
    nav = build_nav(manifests, is_admin=admin)
    path = request.url.path
    base = {
        "manifests": manifests,
        "is_admin": admin,
        "is_tour": is_tour(request),
        "expert": is_expert(request),
        "sunlight": is_sunlight(request),
        "farm_query": explicit_farm(request),
        "nav": nav,
        "nav_current": active_path([i for group in nav.values() for i in group], path),
        "nav_request_path": path + (f"?{request.url.query}" if request.url.query else ""),
    }
    return templates.TemplateResponse(request, name, {**base, **ctx})
