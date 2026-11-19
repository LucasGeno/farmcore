"""Spray products and doses have one copy and one read seam: since core_0018 they live on the
catalog tables, read through `operations.spray_items` and crop protection's `advisor.products`;
the legacy `product` and `quantity` tables are frozen for that revision's rollback and nothing
at runtime reads or writes them.

Blind spots: the guard sees `from farmcore.core.models.canonical import Product|Quantity` only.
It misses a module import (`from farmcore.core.models import canonical`, then
`canonical.Product`), a star import and raw SQL naming the tables. The relationships that reached
them (`log.quantities`, `quantity.product`) are gone from the models, so such access fails."""

from __future__ import annotations

import ast
from pathlib import Path

import farmcore

SRC = Path(farmcore.__file__).parent
LEGACY = {"Product", "Quantity"}
# Modules that still name the frozen models, each for a reason the contract step ends.
ALLOWED = {
    "core/access.py": "declares the frozen tables' read scopes",
}


def _legacy_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "farmcore.core.models.canonical"
        for alias in node.names
        if alias.name in LEGACY
    }


def test_nothing_at_runtime_reads_or_writes_the_frozen_spray_tables() -> None:
    readers = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "migrations" not in path.parts and _legacy_names(path)
    }

    assert readers - ALLOWED.keys() == set(), "read spray products and doses via spray_items"
    assert ALLOWED.keys() - readers == set(), "a listed module no longer needs its exemption"
