"""Spray products and doses have one read seam (`operations.spray_items`, crop protection's
`advisor.products`), so moving them off `product`/`quantity` changes the seam and the writers,
never a reader.

Blind spots: the guard sees `from farmcore.core.models.canonical import Product|Quantity` only.
It misses a module import (`from farmcore.core.models import canonical`, then
`canonical.Product`), a star import, relationship access (`log.quantities`, `q.product`) and the
`legacy_typed_quantity` view. A pass is not proof that every reader moved: a listed module may
also read through those paths (corrections reads `log.quantities` directly)."""

from __future__ import annotations

import ast
from pathlib import Path

import farmcore

SRC = Path(farmcore.__file__).parent
LEGACY = {"Product", "Quantity"}
# Modules that still name the legacy models, each for a reason the contract step ends.
ALLOWED = {
    "apps/operations/spray_items.py": "the seam itself",
    "apps/advisor/products.py": "crop protection's half of the seam",
    "core/access.py": "declares their read scopes",
    "core/logs.py": "writes and merges spray doses",
    "apps/operations/corrections.py": "snapshots and rewrites a spray's doses",
    "apps/operations/seed.py": "writes the demo shortlist",
    "apps/operations/demo.py": "stages a demo spray from the seeded product",
    "apps/advisor/demo.py": "stages demo sprays from the seeded products",
    "apps/inventory/demo_purchases.py": "tops up demo stock of a seeded product",
    "apps/advisor/service.py": "load_sprays and input_snapshot read them until the contract step",
    "core/activity.py": "core cannot import an app; becomes an app contribution",
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


def test_only_the_seam_and_listed_writers_read_spray_products_and_doses() -> None:
    readers = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "migrations" not in path.parts and _legacy_names(path)
    }

    assert readers - ALLOWED.keys() == set(), "read spray products and doses via spray_items"
    assert ALLOWED.keys() - readers == set(), "a listed module no longer needs its exemption"
