"""App registry: discover manifests, order them by dependency, load them into the app.

- `discover(packages)`: core plus every subpackage holding a `manifest.py` with `MANIFEST`,
  returned in load order; registers each app's `resource_scopes` with the scoped repository.
- `resolve_order(manifests)`: Kahn's algorithm, ties alphabetical, core first (every other app
  implicitly depends on it); an app's `after` apps, when present, come before it too. A
  dependency on core is always met, even when core is not in the list, so
  `create_app(manifests=[MANIFEST])` loads just that app (01 contract). Cycles, unmet
  depends and duplicate names raise `RegistryError` naming the apps, so `create_app` fails at
  start.
- `load(app, session_factory, manifests=None)`: upsert `app_module` rows (new -> installed,
  version refreshed), skip uninstalled apps (and apps whose dependency is skipped), import
  models, `configure_mappers()`, include routers (core at the root, apps at `/<name>`), mount
  `/static/<name>` for packages with `static/`.
  `load` writes to the database, so `create_app` (web and bot) needs a reachable, migrated
  database: run `farmcore migrate` before starting either.
- `app.state.manifests`: the loaded apps in load order (navbar, jobs, `GET /`).
  `app.state.all_manifests`: every discovered app in load order, loaded or not (`/apps`).
- `visible_manifests(request)`: the loaded apps that are on now. The UI (nav, Today widgets,
  map layers) renders from this and every app router is gated by it (404 while off); one
  `app_module` read per request, cached on `request.state`.
- `live_active(session, manifests)`: the same rule for any process (assistant tools, drafts,
  replay steps, the worker's planning and live jobs).

Install / uninstall only change `app_module.state` (`switch`: dependency refusals, then the
app's settings model, then the write); a trigger adds one to `app_module.generation` on every
change of state, which fences durable work begun before it (`tasks.publish_if_current`) and
outbox intents are held by their source app (`outbox.may_dispatch`). Off applies at once in every
process; an app off at start mounts its routes at the next start. `resolve_order` refuses an
app built for another `core_api`, an unmet `requires` version or a cycle before any write.
"""

from __future__ import annotations

import heapq
import importlib
import importlib.util
import pkgutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from packaging.specifiers import SpecifierSet
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, configure_mappers, sessionmaker

from farmcore.core.access import register_resource_scopes
from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.manifest import (
    CORE_API,
    Manifest,
    collect_assessment_kinds,
    collect_draft_kinds,
    collect_parcel_views,
    collect_record_types,
    import_string,
)
from farmcore.core.model_spec import collect_model_specs
from farmcore.core.models.base import Base
from farmcore.core.models.kernel import AppModule

# id(manifest) -> (manifest, the package that holds it), for `static/` lookup. Keyed by object
# identity, not name, so a fake app reusing a real app's name keeps its own package; the
# manifest is held so its id is never reused.
_PACKAGES: dict[int, tuple[Manifest, str]] = {id(CORE): (CORE, "farmcore.core")}

INSTALLED = "installed"
UNINSTALLED = "uninstalled"


class RegistryError(RuntimeError):
    """The app graph cannot be loaded (cycle, unmet dependency, duplicate name)."""


def discover(packages: tuple[str, ...] = ("farmcore.apps",)) -> list[Manifest]:
    """Core plus every app package under `packages`, in load order."""
    found = [CORE]
    for pkg_name in packages:
        pkg = importlib.import_module(pkg_name)
        for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
            app_pkg = f"{pkg_name}.{info.name}"
            if not info.ispkg or importlib.util.find_spec(f"{app_pkg}.manifest") is None:
                continue
            manifest = importlib.import_module(f"{app_pkg}.manifest").MANIFEST
            _PACKAGES[id(manifest)] = (manifest, app_pkg)
            found.append(manifest)
    ordered = resolve_order(found)
    for manifest in ordered:
        register_scopes(manifest)
        register_contributions(manifest)
    return ordered


def register_contributions(manifest: Manifest) -> None:
    """Register what core renders for `manifest` without importing it: its activity products
    column, its demo-control counts and the query values its pages accept."""
    from farmcore.core import activity, demo_control, navigation

    if manifest.activity_products:
        activity.PRODUCT_COLUMNS[manifest.name] = manifest.activity_products
    if manifest.demo_counts:
        demo_control.DEMO_COUNTS[manifest.name] = manifest.demo_counts
    for key, values in manifest.query_values:
        navigation.QUERY_VALUES[key] = navigation.QUERY_VALUES.get(key, frozenset()) | set(values)


def register_scopes(manifest: Manifest) -> None:
    """Register `manifest.resource_scopes` with the scoped repository. Runs at discovery, so
    every process that discovers apps (web, bot, replay, migrations) reads app tables the same
    way. An app may declare only tables its own `models` define (imported here to check)."""
    if not manifest.resource_scopes:
        return
    for module in manifest.models:
        importlib.import_module(module)
    owned = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if mapper.class_.__module__ in manifest.models
    }
    register_resource_scopes(manifest.name, manifest.resource_scopes, owned=owned)


def resolve_order(manifests: list[Manifest]) -> list[Manifest]:
    """Dependency order (Kahn's algorithm, ties alphabetical). Core, when present, comes first:
    every other app implicitly depends on it. A dependency on core is met even when core is
    not given.

    Raises `RegistryError` on a duplicate name, an app built for another `core_api`, an unmet
    dependency or version requirement (`Manifest.requires`), or a cycle; `ModelSpecInvalid` on an
    assessment kind whose models are not declared (`core.model_spec`). It runs before any
    write: at start (`load`) and before any migration (`migrations/env.py` discovers first).
    """
    collect_parcel_views(manifests)
    collect_record_types(manifests)
    collect_draft_kinds(manifests)
    collect_assessment_kinds(manifests)
    collect_model_specs(manifests)
    by_name: dict[str, Manifest] = {}
    for m in manifests:
        if m.name in by_name and by_name[m.name] is not m:
            raise RegistryError(f"app {m.name!r} is declared twice")
        by_name[m.name] = m
    for m in sorted(by_name.values(), key=lambda m: m.name):
        if m.core_api != CORE_API:
            raise RegistryError(
                f"app {m.name!r} is built for core_api {m.core_api}; this core is {CORE_API}"
            )
        missing = sorted(set(m.depends) - by_name.keys() - {CORE.name})
        if missing:
            raise RegistryError(
                f"app {m.name!r} depends on {', '.join(map(repr, missing))}, which is not available"
            )
        for dependency, specifier in m.requires:
            found = by_name.get(dependency)
            if found is not None and found.version not in SpecifierSet(specifier):
                raise RegistryError(
                    f"app {m.name!r} needs {dependency!r} {specifier}, "
                    f"but {dependency!r} is {found.version}"
                )
    deps = {name: (set(m.depends) | set(m.after)) & by_name.keys() for name, m in by_name.items()}
    if CORE.name in by_name:
        for name in deps:
            if name != CORE.name:
                deps[name].add(CORE.name)
    ready = [n for n, d in deps.items() if not d]
    heapq.heapify(ready)
    order: list[Manifest] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(by_name[name])
        for other, d in deps.items():
            if name in d:
                d.discard(name)
                if not d:
                    heapq.heappush(ready, other)
    if len(order) < len(by_name):
        cycle = " -> ".join(map(repr, _find_cycle(deps)))
        raise RegistryError(f"dependency cycle between apps: {cycle}")
    return order


def _find_cycle(deps: dict[str, set[str]]) -> list[str]:
    """One cycle among the apps Kahn's algorithm could not place, as `[x, y, z, x]`.

    Every unplaced app still waits on an unplaced dependency, so walking from one
    (deterministically: smallest name first) must revisit an app.
    """
    pending = {name: d for name, d in deps.items() if d}
    path: list[str] = []
    node = min(pending)
    while node not in path:
        path.append(node)
        node = min(pending[node])
    return [*path[path.index(node) :], node]


def sync_modules(session: Session, manifests: list[Manifest]) -> dict[str, str]:
    """Upsert one `app_module` row per manifest (new -> installed; version refreshed) and
    return every manifest's state. Unchanged rows are not touched, so a restart writes nothing.
    Safe when the web and bot processes start together (insert ... on conflict do nothing).
    """
    if manifests:
        session.execute(
            pg_insert(AppModule)
            .values([{"name": m.name, "version": m.version} for m in manifests])
            .on_conflict_do_nothing(index_elements=["name"])
        )
    rows = {
        r.name: r
        for r in session.execute(
            select(AppModule).where(AppModule.name.in_([m.name for m in manifests]))
        ).scalars()
    }
    for m in manifests:
        if rows[m.name].version != m.version:
            rows[m.name].version = m.version
    session.flush()
    return {m.name: rows[m.name].state for m in manifests}


def read_states(session: Session, manifests: list[Manifest]) -> dict[str, str]:
    """Read-only: each manifest's `app_module` state (no row yet -> installed)."""
    rows = {name: state for name, state in session.execute(select(AppModule.name, AppModule.state))}
    return {m.name: rows.get(m.name, INSTALLED) for m in manifests}


def _static_dir(manifest: Manifest) -> Path | None:
    entry = _PACKAGES.get(id(manifest))
    if entry is None:
        return None
    pkg = entry[1]
    path = Path(str(files(pkg) / "static"))
    return path if path.is_dir() else None


def load(
    app: FastAPI,
    session_factory: sessionmaker[Session],
    manifests: list[Manifest] | None = None,
) -> list[Manifest]:
    ordered = resolve_order(discover() if manifests is None else list(manifests))
    for manifest in ordered:
        register_scopes(manifest)
    with session_factory() as session:
        states = sync_modules(session, ordered)
        session.commit()
    active = active_manifests(ordered, states)
    for manifest in active:
        for module in manifest.models:
            importlib.import_module(module)
    configure_mappers()
    for manifest in active:
        prefix = "" if manifest.name == CORE.name else f"/{manifest.name}"
        gate = [] if manifest.name == CORE.name else [Depends(_module_gate(manifest.name))]
        for route in manifest.routes:
            app.include_router(import_string(route), prefix=prefix, dependencies=gate)
        static = _static_dir(manifest)
        if static is not None:
            app.mount(
                f"/static/{manifest.name}",
                StaticFiles(directory=static),
                name=f"static-{manifest.name}",
            )
    app.state.manifests = active
    app.state.all_manifests = ordered
    return active


def _module_gate(name: str):
    """A route dependency: 404 while app `name` is off (`visible_manifests`, this request's one
    read), so a switched-off app's pages and writes are gone at once, not at the next start;
    503 when that read failed."""

    def gate(request: Request) -> None:
        visible = visible_manifests(request)
        if getattr(request.state, "module_states_unread", False):
            # The shell renders with every app (fail-open); an app's own routes fail closed.
            raise HTTPException(status_code=503, detail="App state unavailable")
        if all(m.name != name for m in visible):
            raise HTTPException(status_code=404, detail="Not Found")

    return gate


def live_active(session: Session, manifests: Sequence[Manifest]) -> list[Manifest]:
    """The apps of `manifests` that are on right now, by their live `app_module` state: the one
    rule the web gate, the assistant's tools, drafts, replay steps and the worker's planning
    and live jobs all apply, so every process agrees within one request or tick."""
    ordered = list(manifests)
    return visible_from_states(ordered, read_states(session, ordered))


def active_manifests(ordered: list[Manifest], states: dict[str, str]) -> list[Manifest]:
    """The apps that load: installed, with every dependency loading too (core always loads, and
    counts as loaded even when not given).

    An installed app whose dependency is uninstalled (possible only by editing `app_module`
    directly) is skipped rather than failing the start.
    """
    loaded: set[str] = {CORE.name}
    active: list[Manifest] = []
    for m in ordered:
        if m.name != CORE.name:
            if states.get(m.name, INSTALLED) != INSTALLED and not m.required:
                continue  # a required app loads even if an older build switched it off
            if not set(m.depends) <= loaded:
                continue
        loaded.add(m.name)
        active.append(m)
    return active


def visible_manifests(request: Request) -> list[Manifest]:
    """The loaded apps (load order) whose `app_module` state is not `uninstalled`.

    One query per request, cached on `request.state`. With no session factory, or when the read
    fails, every loaded app counts as visible: the shell still renders (the route gate refuses
    an app's own routes with 503 when the read failed).
    """
    cached = getattr(request.state, "visible_manifests", None)
    if cached is not None:
        return cached
    loaded: list[Manifest] = list(getattr(request.app.state, "manifests", []))
    session_factory = getattr(request.app.state, "session_factory", None)
    off: set[str] = set()
    if session_factory is not None:
        try:
            with session_factory() as session:
                off = set(
                    session.scalars(select(AppModule.name).where(AppModule.state == UNINSTALLED))
                )
        except SQLAlchemyError:
            off = set()
            request.state.module_states_unread = True  # the route gate refuses (503)
    visible = visible_from_states(loaded, {name: UNINSTALLED for name in off})
    request.state.visible_manifests = visible
    return visible


def visible_from_states(manifests: list[Manifest], states: dict[str, str]) -> list[Manifest]:
    """The apps of `manifests` (load order) that are on: core, required apps, and apps whose
    `app_module` state is installed (no row = installed) and whose dependencies among
    `manifests` are on too. A dependency outside `manifests` is not judged here: the list was
    resolved when it was loaded (a caller may pass just the apps it needs).

    The one rule behind the shell and the route gate (`visible_manifests`), the debug drawer's
    panels, the assistant's tools, drafts and the worker (`live_active`), so an app switched off
    on `/apps` disappears from all of them without a restart.
    """
    given = {m.name for m in manifests}
    on: set[str] = {CORE.name}
    visible: list[Manifest] = []
    for m in manifests:
        if m.name != CORE.name and not m.required:
            if states.get(m.name, INSTALLED) != INSTALLED:
                continue
            if any(d in given and d not in on for d in m.depends):
                continue
        on.add(m.name)
        visible.append(m)
    return visible


def join_names(names: list[str]) -> str:
    """`A`, `A and B`, `A, B and C`."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass(frozen=True)
class SwitchRefusal:
    """Why an app cannot be switched: farmer words (app titles), plus the app names involved."""

    detail: str
    extra: dict[str, list[str]] = field(default_factory=dict)


def switch_refusal(
    ordered: list[Manifest], states: dict[str, str], manifest: Manifest, state: str
) -> SwitchRefusal | None:
    """None if `manifest` may be switched to `state` (INSTALLED / UNINSTALLED) given `states`.

    Off: refused for core and required apps, and while an installed app depends on it. On:
    refused while a dependency would not load at the next start (uninstalled, unknown, or
    installed but missing its own dependency: `active_manifests`)."""
    this = manifest.display_name
    if state == UNINSTALLED:
        if manifest.required or manifest.name == CORE.name:
            return SwitchRefusal(f"{this} is always on.", {"dependants": []})
        dependants = [
            m
            for m in ordered
            if manifest.name in m.depends and states.get(m.name, INSTALLED) == INSTALLED
        ]
        if not dependants:
            return None
        titles = [m.display_name for m in dependants]
        if len(titles) == 1:
            detail = f"{titles[0]} needs {this}. Switch off {titles[0]} first."
        else:
            detail = f"{join_names(titles)} need {this}. Switch them off first."
        return SwitchRefusal(detail, {"dependants": [m.name for m in dependants]})
    loading = {m.name for m in active_manifests(ordered, states)} | {CORE.name}
    missing = [d for d in manifest.depends if d not in loading]
    if not missing:
        return None
    by_name = {m.name: m for m in ordered}
    titles = [by_name[d].display_name if d in by_name else d for d in missing]
    if len(titles) == 1:
        detail = f"{this} needs {titles[0]}. Switch on {titles[0]} first."
    else:
        detail = f"{this} needs {join_names(titles)}. Switch them on first."
    return SwitchRefusal(detail, {"uninstalled_dependencies": missing})


def settings_refusal(manifest: Manifest, settings: dict[str, Any]) -> SwitchRefusal | None:
    """None if `settings` satisfy the app's settings model (`Manifest.settings`), checked before
    it is switched on: an app with invalid settings never turns on."""
    if not manifest.settings:
        return None
    try:
        import_string(manifest.settings).model_validate(settings)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
        return SwitchRefusal(
            f"{manifest.display_name} has invalid settings: {', '.join(fields)}.",
            {"invalid_settings": fields},
        )
    return None


def switch(
    session: Session,
    ordered: list[Manifest],
    states: dict[str, str],
    manifest: Manifest,
    state: str,
) -> SwitchRefusal | None:
    """Switch `manifest` to `state` in the session's transaction, or return why not and write
    nothing. The one write path of `/apps` and `farmcore apps`; the database adds one to the
    row's `generation` on every change of state (core_0014), which fences work begun before."""
    refusal = switch_refusal(ordered, states, manifest, state)
    row = session.get(AppModule, manifest.name)
    if refusal is None and state == INSTALLED:
        refusal = settings_refusal(manifest, row.settings if row is not None else {})
    if refusal is not None:
        return refusal
    if row is None:
        row = AppModule(name=manifest.name, version=manifest.version)
        session.add(row)
    if row.state != state:
        row.state = state
    session.flush()
    return None


class _BundleRefused(Exception):
    def __init__(self, refusal: SwitchRefusal) -> None:
        self.refusal = refusal


def bundle_targets(ordered: list[Manifest], names: Sequence[str]) -> list[Manifest]:
    """`names` plus their dependencies, in load order. Unknown names are ignored here: a bundle
    is a static mapping, so `switch_bundle` refuses one instead of silently dropping it."""
    by_name = {m.name: m for m in ordered}
    wanted: set[str] = set()
    pending = [name for name in names if name in by_name]
    while pending:
        name = pending.pop()
        if name in wanted:
            continue
        wanted.add(name)
        pending.extend(d for d in by_name[name].depends if d in by_name)
    return [m for m in ordered if m.name in wanted]


def switch_bundle(
    session: Session,
    ordered: list[Manifest],
    states: dict[str, str],
    names: Sequence[str],
) -> tuple[list[Manifest], SwitchRefusal | None]:
    """Switch on `names` and their dependencies atomically; on refusal nothing is written.

    The dry run runs inside a savepoint, so an app whose settings refuse the switch rolls back
    the apps enabled before it in the same bundle. Returns the bundle's apps in load order and
    the refusal (None on success); callers commit the session themselves.
    """
    by_name = {m.name: m for m in ordered}
    missing = [name for name in names if name not in by_name]
    if missing:
        return [], SwitchRefusal(f"Unknown app: {', '.join(missing)}.")
    targets = bundle_targets(ordered, names)
    working = dict(states)
    try:
        with session.begin_nested():
            for manifest in targets:
                if working.get(manifest.name, INSTALLED) == INSTALLED:
                    continue
                refusal = switch(session, ordered, working, manifest, INSTALLED)
                if refusal is not None:
                    raise _BundleRefused(refusal)
                working[manifest.name] = INSTALLED
    except _BundleRefused as exc:
        return targets, exc.refusal
    return targets, None
