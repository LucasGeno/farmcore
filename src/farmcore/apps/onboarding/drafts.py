"""Crop-assignment drafts: an agent proposes crop seasons the farmer confirms.

`validate` checks a proposed assignment against the farm (its parcels, the FarmCore crop list) and
returns the normalised payload. `confirm` appends one crop season per chosen parcel through the
same `crops.assign` the owner's crops page uses, so history is never rewritten and a covered
parcel keeps its existing season. The agent's text (crop keys, parcel ids) is data: a key is
resolved only through `crops.CROPS`, never guessed.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from farmcore.apps.fields import seasons
from farmcore.apps.onboarding import crops
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm, Parcel


class CropAssignments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_on: date
    assignments: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def non_empty(self):
        if not self.assignments:
            raise ValueError("Name at least one parcel and its crop.")
        return self


def validate(session, farm_id, payload):
    data = CropAssignments.model_validate(payload)
    ids: list[int] = []
    for raw_id in data.assignments:
        if not raw_id.isdigit() or not int(raw_id) < 2**63:
            raise ValueError("Parcel ids must be positive numbers.")
        ids.append(int(raw_id))
    found = set(
        session.scalars(select(Parcel.id).where(Parcel.id.in_(ids), Parcel.farm_id == farm_id))
    )
    if found != set(ids):
        raise ValueError("Some parcels are not on this farm.")
    for key in data.assignments.values():
        if key not in crops.CROPS:
            raise ValueError(f"Unknown crop: {key}.")
    return {"starts_on": data.starts_on.isoformat(), "assignments": data.assignments}


def confirm(session, draft, *, actor, now, services):
    payload = validate(session, draft.farm_id, draft.payload)
    day = date.fromisoformat(payload["starts_on"])
    farm = session.get(Farm, draft.farm_id)
    repo = ScopedRepository(
        session, draft.farm_id, ensure_owner_grant(session, farm), actor, "drafts"
    )
    choices = {int(raw_id): key for raw_id, key in payload["assignments"].items()}
    crops.assign(repo, starts_on=day, choices=choices)
    season = seasons.season_on(repo, min(choices), day)
    return "crop_season", season.id if season is not None else min(choices)
