"""The shell's navigation, built from the loaded manifests.

`build_nav(manifests, is_admin=)` -> `{"farmer", "apps", "more", "admin"}` lists of `NavItem`:
each app's `nav` (in `nav_group`, at `nav_order`) plus its `extra_nav`, sorted by order, ties in
load order. The `apps` group always ends with the kernel's "All apps" page (everyone sees it,
read-only; only the admin can switch apps). The `more` group renders as collapsed Records.
The admin group holds admin-only items and is empty
for non-admins. `web.render` passes only the visible apps (`registry.visible_manifests`).
`active_path(items, path)`: the item path that is the longest prefix of `path`, for
`aria-current`, so `/operations/register` marks Register, not Diary.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from farmcore.core.manifest import NAV_GROUPS, Manifest, NavItem

APPS_ITEM = NavItem("All apps", "/apps", "apps", 99, "apps")


def _items(manifests: Iterable[Manifest]) -> list[NavItem]:
    items: list[NavItem] = []
    for m in manifests:
        if m.nav is not None:
            label, path = m.nav
            items.append(NavItem(label, path, m.nav_group, m.nav_order, m.nav_icon))
        items.extend(m.extra_nav)
    return items


def build_nav(manifests: Iterable[Manifest], *, is_admin: bool) -> dict[str, list[NavItem]]:
    items = [*_items(manifests), APPS_ITEM]
    groups: dict[str, list[NavItem]] = {g: [] for g in NAV_GROUPS}
    for item in sorted(items, key=lambda i: i.order):  # stable: ties keep load order
        if item.group == "admin" and not is_admin:
            continue
        groups[item.group].append(item)
    return groups


def active_path(items: Iterable[NavItem], path: str) -> str | None:
    path = "/" if path in ("/today", "/today/") else path
    best: str | None = None
    best_length = -1
    for item in items:
        configured_path = urlsplit(item.path).path
        matches = path == "/" if configured_path == "/" else path.startswith(configured_path)
        if matches and len(configured_path) > best_length:
            best = item.path
            best_length = len(configured_path)
    return best
