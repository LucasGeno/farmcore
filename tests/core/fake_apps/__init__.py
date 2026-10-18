"""Fake apps for registry tests: `a` depends on `b`, `b` depends on `c`, `c` on nothing.

`CALLS` records every fake seed call as `(app, scene)` so tests can check the order.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

CALLS: list[tuple[str, str | None]] = []


def make_seed(name: str):
    def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
        CALLS.append((name, scene))
        return {f"{name}_rows": len(CALLS)}

    return seed
