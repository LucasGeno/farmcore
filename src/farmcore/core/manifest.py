"""The app contract: every app ships a `MANIFEST` built from these frozen dataclasses.

- Seed: ``seed(session, *, scene: str | None, data_dir: Path) -> dict[str, int]``.
- Job: ``run(ctx: JobContext) -> None``.
- Routes: ``"module:router"``, mounted at ``/<app>`` (core at the root).
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from farmcore.settings import Settings

Maturity = Literal["alpha", "beta", "stable"]
# Where a nav entry renders: `farmer` = phone bottom nav and the sidebar's first group; `apps` =
# an app's own main page (sidebar "Apps", top of the phone "More" sheet); `more` = the Expert
# group (collapsed); `admin` = only with the admin token.
NavGroup = Literal["farmer", "apps", "more", "admin"]
NAV_GROUPS: tuple[NavGroup, ...] = ("farmer", "apps", "more", "admin")
WidgetSpan = Literal["full", "half"]
LayerKind = Literal["parcels", "points"]


@dataclass(frozen=True)
class JobSpec:
    name: str
    func: str  # "module:attr"
    interval: timedelta


@dataclass(frozen=True)
class JobContext:
    session: Session
    now: datetime  # UTC, aware
    settings: Settings
    farm_ids: tuple[int, ...] | None = None  # None = all farms; demo jobs restrict writes


@dataclass(frozen=True)
class NavItem:
    label: str
    path: str
    group: NavGroup = "more"
    order: int = 100  # inside the group; ties keep load order
    icon: str = ""  # a name from `core/_icons.html`; empty = the generic dot

    def __post_init__(self) -> None:
        if self.group not in NAV_GROUPS:
            raise ValueError(f"nav item {self.label!r}: group must be one of {NAV_GROUPS}")


@dataclass(frozen=True)
class Widget:
    """A Today widget: an htmx fragment the Today page loads. `half` pairs up on desktop."""

    url: str
    order: int = 100  # ties keep load order
    span: WidgetSpan = "full"
    label: str = ""  # the slot's name in its error/retry ("Recent activity"); empty = app name


@dataclass(frozen=True)
class MapLayer:
    """A data layer on the farm map, toggled by a chip. `url` returns GeoJSON: `parcels` =
    properties keyed by parcel id that restyle the parcel fill; `points` = features of its own."""

    key: str
    label: str
    url: str
    kind: LayerKind = "parcels"
    order: int = 100
    default_on: bool = False
    icon: str = ""


# Providers cannot overwrite shell identity, routing, context or authorization selectors.
PARCEL_QUERY_RESERVED = frozenset(
    {
        "farm",
        "parcel",
        "parcel_id",
        "view",
        "parent",
        "return_to",
        "token",
        "grant",
        "debug",
        "tour",
        "admin_token",
    }
)


@dataclass(frozen=True)
class ParcelView:
    """An app-owned read-only parcel view; query semantics remain with its provider."""

    key: str
    label: str
    url: str
    fallback_url: str
    order: int = 100
    query_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.key) or self.key == "overview":
            raise ValueError(f"parcel view key {self.key!r} is invalid or reserved")
        if not self.label.strip():
            raise ValueError("parcel view label must not be empty")
        for name, value in (("url", self.url), ("fallback_url", self.fallback_url)):
            parts = urlsplit(value)
            if (
                not value.startswith("/")
                or value.startswith("//")
                or parts.netloc
                or parts.scheme
                or len(value) > 512
                or re.search(r"[\s\\%]", value)
                or any(segment in {".", ".."} for segment in parts.path.split("/"))
                or (
                    parts.fragment
                    and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,95}", parts.fragment)
                )
                or "{id}" not in value
                or "{" in value.replace("{id}", "")
                or "}" in value.replace("{id}", "")
                or (name == "url" and parts.fragment)
            ):
                raise ValueError(f"parcel view {self.key!r}: invalid {name}")
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            if len(dict(pairs)) != len(pairs) or any(
                k in PARCEL_QUERY_RESERVED - {"parcel_id"} or (k == "parcel_id" and v != "{id}")
                for k, v in pairs
            ):
                raise ValueError(f"parcel view {self.key!r}: reserved or duplicate URL query")
        if (
            len(self.query_keys) > 16
            or len(set(self.query_keys)) != len(self.query_keys)
            or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or key in PARCEL_QUERY_RESERVED
                for key in self.query_keys
            )
        ):
            raise ValueError(f"parcel view {self.key!r}: invalid, duplicate or reserved query_keys")


def collect_parcel_views(manifests):
    """Stable explicit order; duplicate keys are a configuration error across loaded apps."""
    views = [(m, view) for m in manifests for view in m.parcel_views]
    keys = [view.key for _, view in views]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate parcel view key across manifests")
    return sorted(views, key=lambda item: item[1].order)


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    summary: str = ""
    depends: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    models: tuple[str, ...] = ()  # modules to import
    jobs: tuple[JobSpec, ...] = ()
    routes: tuple[str, ...] = ()  # "module:router"
    seed: str | None = None  # "module:func"
    nav: tuple[str, str] | None = None  # (label, path)
    maturity: Maturity = "alpha"
    # UX pass. All optional and empty by default; the core renders what apps declare
    # and never imports app code.
    title: str = ""  # the farmer-facing name ("Crop protection"); empty = `name` capitalised
    category: str = ""  # groups tiles on the Apps page
    required: bool = False  # the Apps page offers no switch (the farm needs it to work)
    nav_group: NavGroup = "more"  # where `nav` renders
    nav_order: int = 100  # order inside the group
    nav_icon: str = ""  # icon name for `nav`
    extra_nav: tuple[NavItem, ...] = ()  # more entries, e.g. operations: Diary + Register
    widgets: tuple[Widget, ...] = ()  # Today widgets
    map_layers: tuple[MapLayer, ...] = ()  # data layers on the farm map
    parcel_sections: tuple[str, ...] = ()  # map panel fragment URLs, "{id}" = parcel id
    parcel_views: tuple[ParcelView, ...] = ()
    farm_sections: tuple[str, ...] = ()  # map panel "Farm today" fragment URLs
    debug_panels: tuple[str, ...] = ()  # fragment URLs for the debug drawer
    section_labels: tuple[tuple[str, str], ...] = ()  # optional (section URL, display label)
    data_sources: str | None = None  # provider(repo, now, parcel_id) -> SourceSummary
    resource_scopes: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (table, allowed scopes)
    demo_presets: tuple[str, ...] = ()  # module:attr paths to core.demo.Preset objects
    demo_reset: tuple[str, ...] = ()  # module:attr (session, farm) hooks run by every demo reset
    tools: tuple[Any, ...] = ()  # read-only assistant tools

    def __post_init__(self) -> None:
        keys = [view.key for view in self.parcel_views]
        if len(keys) != len(set(keys)):
            raise ValueError(f"app {self.name!r}: duplicate parcel view key")
        if self.nav_group not in NAV_GROUPS:
            raise ValueError(f"app {self.name!r}: nav_group must be one of {NAV_GROUPS}")
        resources = [resource for resource, _ in self.resource_scopes]
        duplicates = sorted({resource for resource in resources if resources.count(resource) > 1})
        if duplicates:
            raise ValueError(f"app {self.name!r}: duplicate resource scopes for {duplicates}")
        for resource, scopes in self.resource_scopes:
            if not resource or not scopes:
                raise ValueError(
                    f"app {self.name!r}: resource scopes need a table and non-empty scopes"
                )

    @property
    def display_name(self) -> str:
        return self.title or self.name.capitalize()


def import_string(path: str) -> Any:
    """Resolve ``"package.module:attr"`` (attr may be dotted)."""
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"expected 'module:attr', got {path!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj
