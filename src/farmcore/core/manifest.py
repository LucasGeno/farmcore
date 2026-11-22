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

from packaging.specifiers import InvalidSpecifier, SpecifierSet

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from farmcore.settings import Settings

Maturity = Literal["alpha", "beta", "stable"]
CORE_NAME = "core"
# The app contract's version. An app built for another `core_api` is refused before it loads or
# migrates (`registry.resolve_order`).
CORE_API = 1
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


# The one kind that keeps an existing scope: crop protection's envelopes are read with the scope
# that has always read stored advice, so no grant or token changes access and nothing migrates.
_COMPATIBLE_KIND_SCOPES = frozenset({("advisor.spray", "advice:read")})


def collect_assessment_kinds(manifests):
    """Assessment kind key -> (manifest, kind). A duplicate key across loaded apps is an error,
    and so is a read scope two kinds share, or one that already reads something else (a core
    scope, or any app's table scope): each kind is read with its own new scope, so no existing
    holder gains a kind. `_COMPATIBLE_KIND_SCOPES` is the sole exception."""
    from farmcore.core.access import SCOPES  # core's own scopes; access never imports manifests

    manifests = list(manifests)
    taken = set(SCOPES) | {
        scope for m in manifests for _, declared in m.resource_scopes for scope in declared
    }
    kinds = {}
    scopes: dict[str, str] = {}
    for m in manifests:
        for kind in m.assessment_kinds:
            if (
                kind.read_scope in taken
                and (kind.key, kind.read_scope) not in _COMPATIBLE_KIND_SCOPES
            ):
                raise ValueError(
                    f"assessment kind {kind.key!r}: read scope {kind.read_scope!r} already "
                    "reads other data; declare a new scope for the kind"
                )
            if kind.key in kinds:
                raise ValueError(f"duplicate assessment kind key {kind.key!r} across manifests")
            if kind.read_scope in scopes:
                raise ValueError(
                    f"assessment kinds {scopes[kind.read_scope]!r} and {kind.key!r} share the "
                    f"read scope {kind.read_scope!r}; each kind declares its own"
                )
            kinds[kind.key] = (m, kind)
            scopes[kind.read_scope] = kind.key
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
    - confirm: ``(session, draft, *, actor: str, now: datetime, services: dict) -> tuple[str,
      int]``, the record's model and id; raises ``ValueError`` when the draft can no longer be
      recorded. ``services`` holds the caller's composed readers (``spray_weather_reader``).
    - review: a fragment URL, loaded with ``?draft=<id>``.
    - max_payload_bytes: JSON byte cap after validation, default 64 KB, at most 5 MB.
    """

    key: str
    validator: str
    review: str
    confirm: str = ""
    max_payload_bytes: int = 64_000

    def __post_init__(self) -> None:
        _check_key(self.key, "draft kind", reserved=RESERVED_RECORD_TYPE_KEYS)
        if not 1 <= self.max_payload_bytes <= 5_000_000:
            raise ValueError("Draft payload limit must be between 1 and 5000000 bytes.")
        _check_provider(self.validator, "draft kind")
        _check_provider(self.confirm, "draft kind")
        _check_path(self.review, "draft kind")


_ASSESSMENT_KIND = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
# A kind's read scope: read-only by its name, e.g. `assessment:frost.hours:read`.
_KIND_READ_SCOPE = re.compile(r"[a-z][a-z0-9_.]*(:[a-z][a-z0-9_.]*)*:read")


@dataclass(frozen=True)
class AssessmentKind:
    """A typed assessment result an app stores in the shared envelope (`core/assessments.py`).

    - key: ``<app>.<name>``, stored on every envelope of this kind;
    - version: the result shape's version; an envelope is read only by the same version;
    - result: ``module:Model``, a Pydantic model that validates the result when it is stored and
      types it when it is read;
    - models: ``module:SPEC`` paths to the `core.model_spec.ModelSpec` of every model that computes
      it, so its validation level travels with each result. Required for a loaded kind
      (checked at load by `model_spec.collect_model_specs`).
    - read_scope: the scope that reads this kind's envelopes, and only them (`core.access`
      filters `assessment` rows by it). Declared by the app that owns the kind, never by core;
      unique across loaded kinds. A scope core does not know is never granted by default: no
      grant preset, agent token or OAuth connection can hold it (`access.TOKEN_SCOPES`).
    """

    key: str
    version: int
    result: str
    models: tuple[str, ...] = ()
    read_scope: str = ""

    def __post_init__(self) -> None:
        if not _ASSESSMENT_KIND.fullmatch(self.key):
            raise ValueError(f"assessment kind: expected '<app>.<name>', got {self.key!r}")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"assessment kind {self.key!r}: version must be a positive int")
        if not self.result:
            raise ValueError(f"assessment kind {self.key!r}: result model path is required")
        _check_provider(self.result, f"assessment kind {self.key!r}")
        for path in self.models:
            _check_provider(path, f"assessment kind {self.key!r} model")
        if not _KIND_READ_SCOPE.fullmatch(self.read_scope):
            raise ValueError(
                f"assessment kind {self.key!r}: read_scope must be a read scope "
                f"('<name>:read'), got {self.read_scope!r}"
            )


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    summary: str = ""
    depends: tuple[str, ...] = ()
    # Optional apps this one loads (and runs its jobs) after when they are present, without
    # needing them: chat's advice digest runs after crop protection's evaluation, and loads
    # without it. A tuple of app names, never this app or one of its `depends`. An absent name
    # is legitimate (the optional app is not in the bundle), so a typo is caught by the test
    # that every shipped `after` names a discovered app, not at start.
    after: tuple[str, ...] = ()
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
    # Contributions core renders without importing the app (filled at discovery, `registry`):
    activity_products: str | None = None  # (repo, log_ids) -> {log id: ["copper 2 kg/ha"]}
    demo_counts: str | None = None  # (session, farm, now) -> {label: count} on demo control
    query_values: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (query key, allowed values)
    tools: tuple[Any, ...] = ()  # read-only assistant tools
    # Farmer-first experience (F1/F3): the app grid, search and Record sheet read these.
    icon: str = ""  # a core icon name, or an app-shipped SVG path
    color: str = ""  # a core palette token
    keywords: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # locale -> synonyms
    tabs: tuple[Tab, ...] = ()  # the app's own top navigation
    search: str = ""  # provider "module:func" returning typed results
    record_types: tuple[RecordType, ...] = ()  # facts the Record sheet can capture
    draft_kinds: tuple[DraftKind, ...] = ()  # validator + review fragment per draft kind
    assessment_kinds: tuple[AssessmentKind, ...] = ()  # typed results kept in the envelope
    # "module:SPEC" paths of models the app shows beside its results without storing them (a
    # comparison model): listed on the About pages, never stamped on a stored result.
    model_specs: tuple[str, ...] = ()
    attention: str = ""  # provider "module:func" returning at most N home-strip items
    # Weather providers (core.weather): "module:factory", factory(settings) -> WeatherProvider.
    weather_providers: tuple[str, ...] = ()
    # Outbox deliveries (core.outbox): (intent kind, "module:factory"), factory(settings) ->
    # a provider, or None when unconfigured (its intents then wait, and expire).
    delivery_providers: tuple[tuple[str, str], ...] = ()
    # Module lifecycle (`registry`): the app contract version it was built for; version
    # requirements on its `depends`, as (app, PEP 440 specifier), e.g. (("forecast", ">=0.3"),);
    # and a "module:Model" Pydantic model its `app_module.settings` must satisfy to switch on.
    core_api: int = CORE_API
    requires: tuple[tuple[str, str], ...] = ()
    settings: str = ""

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
        if not isinstance(self.after, tuple) or not all(
            isinstance(name, str) and name for name in self.after
        ):
            raise ValueError(f"app {self.name!r}: after must be a tuple of app names")
        if self.name in self.after:
            raise ValueError(f"app {self.name!r}: after names the app itself")
        if both := sorted(set(self.after) & set(self.depends)):
            raise ValueError(f"app {self.name!r}: after repeats depends {both}")
        _check_icon(self.icon, f"app {self.name!r}", app_name=self.name)
        if self.color and self.color not in COLOR_TOKENS:
            raise ValueError(
                f"app {self.name!r}: color {self.color!r} is not a palette token"
                f" {sorted(COLOR_TOKENS)}"
            )
        _check_provider(self.search, f"app {self.name!r} search")
        _check_provider(self.attention, f"app {self.name!r} attention")
        if not isinstance(self.weather_providers, tuple) or not all(
            isinstance(path, str) and path for path in self.weather_providers
        ):
            raise ValueError(f"app {self.name!r}: weather_providers must be a tuple of paths")
        for path in self.weather_providers:
            _check_provider(path, f"app {self.name!r} weather provider")
        for kind, path in self.delivery_providers:
            _check_provider(path, f"app {self.name!r} delivery provider {kind!r}")
        for job in self.jobs:
            if job.task is not None and not job.name.startswith(f"{self.name}."):
                # The task fence finds the task's module by this prefix (`tasks.module_of`).
                raise ValueError(
                    f"app {self.name!r}: durable job {job.name!r} must be named '{self.name}.<job>'"
                )
        _check_provider(self.settings, f"app {self.name!r} settings")
        for path in self.model_specs:
            _check_provider(path, f"app {self.name!r} model spec")
        for dependency, specifier in self.requires:
            if dependency not in self.depends:
                raise ValueError(
                    f"app {self.name!r}: requires {dependency!r}, which is not in depends"
                )
            try:
                SpecifierSet(specifier)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"app {self.name!r}: invalid version requirement {specifier!r}"
                ) from exc
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
