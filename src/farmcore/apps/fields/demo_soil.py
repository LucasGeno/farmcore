"""Offline topsoil estimates for the public IGN demo parcels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from farmcore.apps.fields.seed import IGN_SOURCE
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import ExtId, Parcel
from farmcore.settings import Settings

SNAPSHOT = Path("soilgrids/demo_topsoil_ph.json")
PHValue = Annotated[float, Field(strict=True, ge=0, le=14)]


class TopsoilPH(BaseModel):
    min_ph: PHValue
    max_ph: PHValue
    method: Literal["polygon", "point"]


def topsoil_for(repo: ScopedRepository, parcel: Parcel, settings: Settings) -> TopsoilPH | None:
    if (
        not settings.demo_mode
        or repo.farm_id not in settings.demo_farm_ids
        or parcel.farm_id != repo.farm_id
        or not parcel.iacs_id
    ):
        return None

    identity = select(ExtId.key).where(
        ExtId.source == IGN_SOURCE,
        ExtId.model == Parcel.__tablename__,
        ExtId.res_id == parcel.id,
        ExtId.key == parcel.iacs_id,
    )
    key = repo.execute(identity, scope="fields:read", resource="ext_id").scalar_one_or_none()
    if key is None:
        return None

    try:
        rows = json.loads((settings.data_dir / SNAPSHOT).read_text(encoding="utf-8"))
        if not isinstance(rows, dict) or key not in rows:
            return None
        estimate = TopsoilPH.model_validate(rows[key])
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        return None

    return estimate if estimate.min_ph <= estimate.max_ph else None
