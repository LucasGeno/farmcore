"""The app contract: every app ships a `MANIFEST` built from these frozen dataclasses.

- Seed: ``seed(session, *, scene: str | None, data_dir: Path) -> dict[str, int]``.
- Job: ``run(ctx: JobContext) -> None``.
- Routes: ``"module:router"``, mounted at ``/<app>`` (core at the root).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from farmcore.settings import Settings

Maturity = Literal["alpha", "beta", "stable"]
CORE_NAME = "core"
# Core icon names from `core/templates/core/_icons.html`; an app `icon` is one of these, or a safe
# SVG path the app ships.
ICON_NAMES = frozenset(
    {
        "home",
        "calendar",
        "bell",
        "database",
        "today",
        "map",
        "register",
        "diary",
        "more",
        "advisor",
        "station",
        "chart",
        "history",
        "landmark",
        "chat",
        "share",
        "apps",
        "spray",
        "protected",
        "wait",
        "none",
        "mic",
        "tractor",
        "chevron-right",
        "chevron-left",
        "alert",
        "chevron-down",
        "clock",
        "rain",
        "thermo",
        "wind",
        "dry",
        "humidity",
        "check",
        "x",
        "leaf",
        "info",
        "bug",
        "box",
        "clipboard",
    }
)
# The shell's colour tokens an app tile may use as its accent (farmcore.css / tailwind.config.js).
COLOR_TOKENS = frozenset({"brand", "spray", "protected", "wait", "ink", "muted"})
# Record-type (and draft-kind) keys the Record sheet reserves for itself.
RESERVED_RECORD_TYPE_KEYS = frozenset({"overview"})
# Where a nav entry renders: `farmer` = phone bottom nav and the sidebar's first group (core only);
# `apps` = an app's own main page (sidebar "Apps", top of the phone "More" sheet); `more` = the
# Expert group (collapsed); `admin` = only with the admin token.
NavGroup = Literal["farmer", "apps", "more", "admin"]
NAV_GROUPS: tuple[NavGroup, ...] = ("farmer", "apps", "more", "admin")
WidgetSpan = Literal["full", "half"]
LayerKind = Literal["parcels", "points"]


@dataclass(frozen=True)
class JobSpec:
    """A job runs `func(ctx: JobContext)` every `interval`. With `task` it is durable live work
    (`core/tasks.py`): in live mode `func` only enqueues one task per due farm in `ctx.session`,
    and `farmcore worker` runs `task(ctx: TaskContext)`, which may publish only before its
    attempt's `deadline`. In replay `func` runs in process, as for every job."""

    name: str
    func: str  # "module:attr"
    interval: timedelta
    task: str | None = None  # "module:attr"
    deadline: timedelta = timedelta(minutes=1)


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


def collect_draft_kinds(manifests):
    """Draft kind key -> (manifest, kind); a duplicate key across loaded apps is an error."""
    kinds = {}
    for m in manifests:
        for kind in m.draft_kinds:
            if kind.key in kinds:
                raise ValueError(f"duplicate draft kind key {kind.key!r} across manifests")
            kinds[kind.key] = (m, kind)
    return kinds


def collect_record_types(manifests):
    """Record types in load order; a duplicate key across loaded apps is a configuration error."""
    types = [(m, rt) for m in manifests for rt in m.record_types]
    keys = [rt.key for _, rt in types]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate record type key across manifests")
    return types


class FrozenMapping(Mapping[str, tuple[str, ...]]):
    """An immutable, hashable mapping, so the frozen `Manifest` stays hashable.

    `MappingProxyType` stays unhashable (it delegates to its dict); this wraps the same data in a
    tuple-of-pairs so both ``m.keywords[locale]`` and ``hash(m)`` work.
    """

    __slots__ = ("_items", "_hash")

    def __init__(self, data: Mapping[str, tuple[str, ...]]):
        self._items = tuple(sorted(data.items()))
        self._hash = hash(self._items)

    def __getitem__(self, key: str) -> tuple[str, ...]:
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self):
        return (k for k, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenMapping):
            return self._items == other._items
        if isinstance(other, Mapping):
            return dict(self._items) == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"FrozenMapping({dict(self._items)!r})"


_KEY = re.compile(r"[a-z][a-z0-9-]{0,31}")
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_FRAGMENT = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,95}")


def _check_key(value: str, field: str, *, reserved: frozenset[str]) -> None:
    if not _KEY.fullmatch(value) or value in reserved:
        raise ValueError(f"{field} key {value!r} is invalid or reserved")


def _check_path(value: str, field: str, *, allow_fragment: bool = True) -> None:
    """A local URL path: no scheme, netloc, whitespace, percent or `..` segment (ParcelView)."""
    parts = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parts.netloc
        or parts.scheme
        or len(value) > 512
        or re.search(r"[\s\\%]", value)
        or any(segment in {".", ".."} for segment in parts.path.split("/"))
        or (parts.fragment and not _FRAGMENT.fullmatch(parts.fragment))
        or (not allow_fragment and parts.fragment)
    ):
        raise ValueError(f"{field}: invalid path")


def _check_provider(value: str, field: str) -> None:
    """``"module:attr"`` (attr may be dotted); resolved lazily, only the shape is checked."""
    if not value:
        return
    module, sep, attr = value.partition(":")
    if (
        not sep
        or not module
        or not attr
        or not _DOTTED.fullmatch(module)
        or not _DOTTED.fullmatch(attr)
    ):
        raise ValueError(f"{field}: expected 'module:attr', got {value!r}")


def _check_app_prefix(value: str, app_name: str, field: str) -> None:
    if not value.startswith(f"/{app_name}/"):
        raise ValueError(f"{field}: path must be under '/{app_name}/'")


def _check_icon(value: str, field: str, *, app_name: str | None = None) -> None:
    """An icon is a core icon name, an app-shipped SVG path, or empty.

    A path icon must be the app's own asset: ``/static/<app>/...svg`` with no query or fragment,
    so one app cannot point a grid tile at another app's asset or an arbitrary GET route.
    """
    if not value or value in ICON_NAMES:
        return
    if not value.startswith("/"):
        raise ValueError(f"{field}: unknown icon name or unsafe path {value!r}")
    _check_path(value, f"{field} icon", allow_fragment=False)
    if app_name is not None:
        parts = urlsplit(value)
        if (
            not value.startswith(f"/static/{app_name}/")
            or not value.endswith(".svg")
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                f"{field}: icon path must be '/static/{app_name}/...svg' with no query or fragment"
            )


@dataclass(frozen=True)
class Tab:
    """An app's own top navigation entry, rendered inside the app, not in the shell chrome."""

    label: str
    path: str

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("tab label must not be empty")
        _check_path(self.path, "tab")


@dataclass(frozen=True)
class RecordType:
    """One fact an app can record from the Record sheet: key, label, icon, form fragment URL and
    the draft kind that validates it."""

    key: str
    label: str
    icon: str = ""
    form: str = ""  # the form fragment URL
    draft_kind: str = ""  # a key from the manifest's `draft_kinds`

    def __post_init__(self) -> None:
        _check_key(self.key, "record type", reserved=RESERVED_RECORD_TYPE_KEYS)
        if not self.label.strip():
            raise ValueError("record type label must not be empty")
        if self.form:
            _check_path(self.form, "record type")


@dataclass(frozen=True)
class DraftKind:
    """A draft kind: a validator (``module:func``), its review fragment URL and the app-owned
    confirm service (``module:func``) that turns a confirmed draft into a record.

    - validator: ``(session, farm_id, payload: dict) -> dict``, the normalised payload; raises
      ``ValueError`` with a farmer-readable reason.
    - confirm: ``(session, draft, *, actor: str, now: datetime) -> tuple[str, int]``, the record's
      model and id; raises ``ValueError`` when the draft can no longer be recorded.
    - review: a fragment URL, loaded with ``?draft=<id>``.
    """

    key: str
    validator: str
    review: str
    confirm: str = ""

    def __post_init__(self) -> None:
        _check_key(self.key, "draft kind", reserved=RESERVED_RECORD_TYPE_KEYS)
        _check_provider(self.validator, "draft kind")
        _check_provider(self.confirm, "draft kind")
        _check_path(self.review, "draft kind")


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
    # Farmer-first experience (F1/F3): the app grid, search and Record sheet read these.
    icon: str = ""  # a core icon name, or an app-shipped SVG path
    color: str = ""  # a core palette token
    keywords: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # locale -> synonyms
    tabs: tuple[Tab, ...] = ()  # the app's own top navigation
    search: str = ""  # provider "module:func" returning typed results
    record_types: tuple[RecordType, ...] = ()  # facts the Record sheet can capture
    draft_kinds: tuple[DraftKind, ...] = ()  # validator + review fragment per draft kind
    attention: str = ""  # provider "module:func" returning at most N home-strip items

    def __post_init__(self) -> None:
        keys = [view.key for view in self.parcel_views]
        if len(keys) != len(set(keys)):
            raise ValueError(f"app {self.name!r}: duplicate parcel view key")
        if self.nav_group not in NAV_GROUPS:
            raise ValueError(f"app {self.name!r}: nav_group must be one of {NAV_GROUPS}")
        if self.name != CORE_NAME:
            if self.nav_group == "farmer":
                raise ValueError(
                    f"app {self.name!r}: only core may add a phone bottom-nav slot"
                    " (nav_group='farmer')"
                )
            if any(item.group == "farmer" for item in self.extra_nav):
                raise ValueError(
                    f"app {self.name!r}: only core may add a phone bottom-nav slot"
                    " (extra_nav, group='farmer')"
                )
        _check_icon(self.icon, f"app {self.name!r}", app_name=self.name)
        if self.color and self.color not in COLOR_TOKENS:
            raise ValueError(
                f"app {self.name!r}: color {self.color!r} is not a palette token"
                f" {sorted(COLOR_TOKENS)}"
            )
        _check_provider(self.search, f"app {self.name!r} search")
        _check_provider(self.attention, f"app {self.name!r} attention")
        for locale, words in self.keywords.items():
            if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2,4})?", locale):
                raise ValueError(f"app {self.name!r}: invalid keyword locale {locale!r}")
            if not isinstance(words, tuple) or any(not isinstance(w, str) for w in words):
                raise ValueError(
                    f"app {self.name!r}: keyword synonyms for {locale!r} must be a tuple of strings"
                )
            if not words or any(not w.strip() or w != w.strip() for w in words):
                raise ValueError(
                    f"app {self.name!r}: keyword synonyms for {locale!r} must be non-empty"
                )
            if len(set(words)) != len(words):
                raise ValueError(f"app {self.name!r}: duplicate keyword synonyms for {locale!r}")
        object.__setattr__(self, "keywords", FrozenMapping(self.keywords))
        if len({tab.path for tab in self.tabs}) != len(self.tabs):
            raise ValueError(f"app {self.name!r}: duplicate tab path")
        for tab in self.tabs:
            _check_app_prefix(tab.path, self.name, f"app {self.name!r} tab")
        record_keys = [rt.key for rt in self.record_types]
        if len(record_keys) != len(set(record_keys)):
            raise ValueError(f"app {self.name!r}: duplicate record type key")
        for rt in self.record_types:
            _check_icon(rt.icon, f"app {self.name!r} record type", app_name=self.name)
            if rt.form:
                _check_app_prefix(rt.form, self.name, f"app {self.name!r} record type")
        draft_kind_keys = {dk.key for dk in self.draft_kinds}
        for dk in self.draft_kinds:
            _check_app_prefix(dk.review, self.name, f"app {self.name!r} draft kind")
        for rt in self.record_types:
            if rt.draft_kind and rt.draft_kind not in draft_kind_keys:
                raise ValueError(
                    f"app {self.name!r}: record type {rt.key!r} names undeclared"
                    f" draft_kind {rt.draft_kind!r}"
                )
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
