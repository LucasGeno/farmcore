"""App registry: discover manifests, order them by dependency, load them into the app.

- `discover(packages)`: core plus every subpackage holding a `manifest.py` with `MANIFEST`,
  returned in load order.
- `resolve_order(manifests)`: Kahn's algorithm, ties alphabetical, core first (every other app
  implicitly depends on it). A dependency on core is always met, even when core is not in the
  list, so `create_app(manifests=[MANIFEST])` loads just that app (01 contract). Cycles, unmet
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
- `visible_manifests(request)`: the loaded apps the farm has not switched off. The UI
  (nav, Today widgets, map layers) renders from this, so switching an app off or on shows at
  once; one `app_module` read per request, cached on `request.state`.

Install / uninstall only change `app_module.state`. The UI follows at once for loaded apps;
routes, jobs and models follow at the next start. `switch_refusal` is the one rule for which
switches are allowed, shared by `/apps` and `farmcore apps`.
"""

from __future__ import annotations

import heapq
import importlib
import importlib.util
import pkgutil
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, configure_mappers, sessionmaker

from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.manifest import Manifest, collect_parcel_views, import_string
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
    return resolve_order(found)


def resolve_order(manifests: list[Manifest]) -> list[Manifest]:
    """Dependency order (Kahn's algorithm, ties alphabetical). Core, when present, comes first:
    every other app implicitly depends on it. A dependency on core is met even when core is
    not given.

    Raises `RegistryError` on a duplicate name, an unmet dependency or a cycle.
    """
    collect_parcel_views(manifests)
    by_name: dict[str, Manifest] = {}
    for m in manifests:
        if m.name in by_name and by_name[m.name] is not m:
            raise RegistryError(f"app {m.name!r} is declared twice")
        by_name[m.name] = m
    for m in sorted(by_name.values(), key=lambda m: m.name):
        missing = sorted(set(m.depends) - by_name.keys() - {CORE.name})
        if missing:
            raise RegistryError(
                f"app {m.name!r} depends on {', '.join(map(repr, missing))}, which is not available"
            )
    deps = {name: set(m.depends) & by_name.keys() for name, m in by_name.items()}
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
        for route in manifest.routes:
            app.include_router(import_string(route), prefix=prefix)
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
    fails, every loaded app counts as visible: the shell still renders.
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
    visible = visible_from_states(loaded, {name: UNINSTALLED for name in off})
    request.state.visible_manifests = visible
    return visible


def visible_from_states(manifests: list[Manifest], states: dict[str, str]) -> list[Manifest]:
    """The loaded apps (load order) a visitor sees: core, required apps, and every app whose
    `app_module` state is not `uninstalled` (no row = installed).

    The one rule behind the shell (`visible_manifests`), the debug drawer's panels and the
    chat assistant's tools, so an app switched off on `/apps` disappears from all three without a
    restart.
    """
    return [
        m
        for m in manifests
        if m.name == CORE.name or m.required or states.get(m.name, INSTALLED) != UNINSTALLED
    ]


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
