"""Spray products and doses have one read seam (`operations.spray_items`, crop protection's
`advisor.products`), so moving them off `product`/`quantity` changes the seam and the writers,
never a reader."""

from __future__ import annotations

import ast
from pathlib import Path

import farmcore

SRC = Path(farmcore.__file__).parent
LEGACY = {"Product", "Quantity"}
# Modules that still name the legacy models, each for a reason the contract step (14c-2) ends.
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
    "apps/advisor/service.py": "load_sprays and input_snapshot; shared with #125, moves in 14c-2",
    "core/activity.py": "core cannot import an app; becomes an app contribution in 14c-2",
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
