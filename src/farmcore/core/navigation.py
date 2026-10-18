"""Bounded reading origins, separate from general-purpose redirect helpers."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from fastapi import Request
from pydantic import TypeAdapter

from farmcore.core.access import ScopedRepository
from farmcore.core.activity import ActivityType
from farmcore.core.manifest import collect_parcel_views
from farmcore.core.models.canonical import Parcel
from farmcore.core.registry import visible_manifests
from farmcore.core.urls import with_farm


@dataclass(frozen=True)
class ReturnDestination:
    url: str
    label: str


def _parts(value: str):
    """Parse once, rejecting browser/parser normalization and ambiguous encodings."""
    if (
        not value
        or len(value) > 2048
        or not value.startswith("/")
        or value.startswith("//")
        or re.search(r"[\s\\\x00-\x1f\x7f]", value)
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise ValueError("Invalid reading origin")
    decoded = unquote(value, errors="strict")
    if re.search(r"[\\\x00-\x1f\x7f]", decoded):
        raise ValueError("Ambiguous reading origin")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or "%" in parts.path:
        raise ValueError("Not a local reading path")
    return parts


def map_read_url(request: Request, path: str, parcel_id: int) -> str:
    """The known Map parent, including after async loads, polling and Retry."""
    farm_id = request.state.selected_farm_id
    origin = with_farm(f"/fields/?parcel={parcel_id}#map-panel-heading", farm_id)
    target = urlsplit(with_farm(path, farm_id))
    query = parse_qsl(target.query) + [("return_to", origin)]
    return target._replace(query=urlencode(query)).geturl()


def resolved_url(request: Request, path: str) -> str:
    """Freeze the farm already authorized by this reading request."""
    return with_farm(path, request.state.selected_farm_id)


_ROUTES = {
    "/": ("today", "Home", set()),
    "/today/": ("today", "Home", set()),
    "/fields/": ("fields", "Map", {"parcel", "view", "q"}),
    "/advisor/": ("advisor", "Crop protection", {"model"}),
    "/forecast/plan": ("forecast", "Plan", {"day"}),
    "/activity/": (
        "activity",
        "Activity",
        {"parcel_id", "type", "from", "to", "before", "before_id"},
    ),
}
_ACTIVITY_KEYS = {"type", "from", "to", "before", "before_id"}
_PARCEL_ROUTE = re.compile(r"/(fields|operations)/parcels/([1-9][0-9]{0,9})")
_DATE = TypeAdapter(date)
_DATETIME = TypeAdapter(datetime)
_KIND = TypeAdapter(ActivityType)
_MODEL = TypeAdapter(Literal["all", "downy", "powdery"])


def _parcel(repo: ScopedRepository, value: str) -> bool:
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return False
    parcel_id = int(value)
    return parcel_id < 2**31 and repo.get(Parcel, parcel_id, scope="fields:read") is not None


def _activity(query: dict[str, str]) -> None:
    if "type" in query:
        _KIND.validate_python(query["type"])
    for key in ("from", "to"):
        if query.get(key):
            _DATE.validate_python(query[key])
    if (
        query.get("from")
        and query.get("to")
        and _DATE.validate_python(query["from"]) > _DATE.validate_python(query["to"])
    ):
        raise ValueError("Reversed period")
    if "before" in query:
        _DATETIME.validate_python(query["before"])
    if "before_id" in query and (
        "before" not in query
        or not re.fullmatch(r"[0-9]{1,19}", query["before_id"])
        or int(query["before_id"]) >= 2**63
    ):
        raise ValueError("Invalid cursor")


def reading_destination(
    request: Request, repo: ScopedRepository, value: str
) -> ReturnDestination | None:
    """Validate navigation metadata without selecting a farm or visiting its destination."""
    try:
        parts = _parts(value)
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(query) != len(pairs) or ("farm" in query and query["farm"] != str(repo.farm_id)):
            return None
        if any("%" in value for key, value in pairs if key != "q"):
            return None
        route = _ROUTES.get(parts.path)
        match = _PARCEL_ROUTE.fullmatch(parts.path)
        visible = visible_manifests(request)
        if match:
            app, parcel_id = match.groups()
            if not _parcel(repo, parcel_id):
                return None
            if app == "fields":
                view = query.get("view", "overview")
                loaded = getattr(request.app.state, "all_manifests", visible)
                descriptor = next(
                    (v for _, v in collect_parcel_views(loaded) if v.key == view), None
                )
                allowed = {"view"} | (set(descriptor.query_keys) if descriptor else set())
                if query.keys() - allowed - {"farm"}:
                    return None
                if any(not bounded_query(value) for key, value in query.items() if key != "farm"):
                    return None
                if view != "overview" and descriptor is None:
                    return None
                if view != "overview" and view not in {
                    v.key for _, v in collect_parcel_views(visible)
                }:
                    return ReturnDestination(
                        with_farm(f"/fields/?parcel={parcel_id}#map-panel-heading", repo.farm_id),
                        "Back to Map",
                    )
                route = (app, "parcel", allowed)
            else:
                route = (app, "spray history", set())
        if route is None:
            return None
        app, label, allowed = route
        if query.keys() - allowed - {"farm"}:
            return None
        if parts.path == "/fields/":
            mode = query.get("view", "map")
            if mode not in {"map", "list"} or len(query.get("q", "")) > 80:
                return None
            if (mode == "list" and "parcel" in query) or (mode == "map" and "q" in query):
                return None
            label = "Parcels" if mode == "list" else "Map"
        for key in ("parcel", "parcel_id"):
            if key in query and not _parcel(repo, query[key]):
                return None
        if parts.path == "/forecast/plan" and "day" in query:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", query["day"]):
                return None
            _DATE.validate_python(query["day"])
        if "model" in query:
            _MODEL.validate_python(query["model"])
        if parts.path == "/activity/":
            _activity(query)
        if parts.fragment:
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,95}", parts.fragment):
                return None
            if parts.path == "/forecast/plan" and parts.fragment != "plan-heading":
                if not parts.fragment.startswith("plan-day-"):
                    return None
                _DATE.validate_python(parts.fragment.removeprefix("plan-day-"))
        if app not in {m.name for m in visible}:
            return ReturnDestination(with_farm("/fields/", repo.farm_id), "Back to Map")
        return ReturnDestination(with_farm(value, repo.farm_id), "Back to " + label)
    except (ValueError, OverflowError):
        return None


def return_destination(
    request: Request,
    repo: ScopedRepository,
    fallback_url: str,
    fallback_label: str,
) -> ReturnDestination:
    """One optional origin; malformed/recursive context retains legacy entry semantics."""
    values = request.query_params.getlist("return_to")
    if len(values) == 1 and (target := reading_destination(request, repo, values[0])):
        return target
    return ReturnDestination(with_farm(fallback_url, repo.farm_id), fallback_label)


def is_contextual_detail(value: str) -> bool:
    """Only newly contextual legacy details opt into clearing selection on a farm switch."""
    parts = urlsplit(value)
    return bool(
        re.fullmatch(r"/fields/parcels/[1-9][0-9]{0,9}", parts.path)
        or (
            re.fullmatch(r"/(advisor|operations)/parcels/[1-9][0-9]{0,9}", parts.path)
            and any(key == "return_to" for key, _ in parse_qsl(parts.query))
        )
    )


def farm_switch_parent(value: str) -> str | None:
    """A detail's collection in the new farm, without the old farm's resource state."""
    if re.fullmatch(r"/stations/[1-9][0-9]*", urlsplit(value).path):
        return "/stations/"
    if is_contextual_detail(value):
        return "/fields/"
    return None


def bounded_query(value: str) -> bool:
    """The shell limits transport state; each provider owns semantic validation."""
    return len(value) <= 256 and not re.search(r"[\x00-\x1f\x7f\\%]", value)


def workspace_parent(
    request: Request, repo: ScopedRepository, parcel_id: int, view: str
) -> str | None:
    """Validate the shell's canonical parent before an app uses it in forms or reading links.

    One separately validated original origin may accompany the workspace. It is never itself
    accepted as a recursive return destination by `reading_destination`.
    """
    values = request.query_params.getlist("parent")
    if len(values) != 1:
        return None
    try:
        value = values[0]
        # The parent may contain a once-encoded original URL, including a literal List search.
        if (
            len(value) > 2048
            or not value.startswith("/")
            or value.startswith("//")
            or re.search(r"[\s\\\x00-\x1f\x7f]", value)
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            return None
        parts = urlsplit(value)
        if (
            parts.scheme
            or parts.netloc
            or parts.fragment
            or parts.path != f"/fields/parcels/{parcel_id}"
        ):
            return None
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(query) != len(pairs) or query.get("view", "overview") != view:
            return None
        original = query.pop("return_to", None)
        plain = parts._replace(query=urlencode(query)).geturl()
        valid = reading_destination(request, repo, plain)
        if valid is None or urlsplit(valid.url).path != parts.path:
            return None
        if original is not None:
            origin = reading_destination(request, repo, original)
            if origin is None:
                return None
            query["return_to"] = origin.url
        return parts._replace(query=urlencode({**query, "farm": str(repo.farm_id)})).geturl()
    except (ValueError, OverflowError):
        return None
