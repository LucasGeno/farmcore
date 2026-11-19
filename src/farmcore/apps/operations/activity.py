"""Operations' contribution to the farm activity (`Manifest.activity_products`): the products
column, `copper 2 kg/ha` per rate that names a product, by its active substance."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from farmcore.apps.operations import spray_items
from farmcore.core.access import ScopedRepository

SCOPE = "logs:read"


def _dose(value: Any) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def products(repo: ScopedRepository, log_ids: Sequence[int]) -> dict[int, list[str]]:
    """log id -> one line per rate that names a product (a matched item or a label)."""
    doses = spray_items.doses_for_logs(repo, log_ids, scope=SCOPE, rates_only=True)
    items = spray_items.items_by_id(
        repo, {d.item_id for ds in doses.values() for d in ds}, scope=SCOPE
    )
    out: dict[int, list[str]] = {}
    for log_id, ds in doses.items():
        for d in ds:
            if d.item_id is not None:
                item = items.get(d.item_id)
                name = (item.active_substance or item.name) if item else None
            else:
                name = d.label
            if name:
                out.setdefault(log_id, []).append(f"{name} {_dose(d.value)} {d.unit}")
    return out
