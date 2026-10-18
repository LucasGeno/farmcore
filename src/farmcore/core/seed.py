"""Core seed: the demo farm ("Château demo") and its owner grant, before any app seed."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from farmcore.core import ext_ids
from farmcore.core.access import DEMO_SOURCE, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.settings import Settings, get_settings

DEMO_FARM_NAME = "Château demo"


def ensure_default_farm(session: Session, settings: Settings | None = None) -> Farm:
    """Get-or-create the demo farm (ext_id `farmcore-demo`:`default_farm_key`) and its owner
    grant. Idempotent; every app seed may call it."""
    settings = settings or get_settings()
    ext_ids.ensure_source(session, DEMO_SOURCE, "farmcore demo data", "seed", "Apache-2.0")
    farm = ext_ids.lookup(session, DEMO_SOURCE, settings.default_farm_key)
    if not isinstance(farm, Farm):
        farm, _ = ext_ids.upsert(
            session,
            source=DEMO_SOURCE,
            key=settings.default_farm_key,
            model=Farm,
            values={"name": DEMO_FARM_NAME},
        )
    ensure_owner_grant(session, farm)
    return farm


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    ensure_default_farm(session)
    return {"farm": 1}
